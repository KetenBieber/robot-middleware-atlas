# Resource Tree 与 Route Cache：从 Key Expression 到可复用路由

设仓库里 80 台移动机器人各发布关节、位姿和诊断数据；KeyExpr 总数达到数千，订阅端还会用 `robot/**` 监视整组设备。朴素路由器每收到一份 20 Hz 状态，就扫描所有声明并重新判断表达式是否相交；随着设备增多，路由线程的 CPU 时间线性爬升，队列年龄变大，控制器拿到的位姿越来越旧。若拓扑变更后还沿用上一次目的地列表，订阅端会在断线重连后继续等待旧 Face，表现为状态停更。这里的数量和速率是教学量级，不是项目基准测试。Zenoh 把问题拆成两层：Resource tree 保存表达式及相交关系，带版本号的 Route cache 保存“从某个来源出发应该去哪些目的地”。

KeyExpr 是分段的逻辑资源名，`*` 和 `**` 可表达一段或多段通配；“相交”指两个表达式至少能表示同一个完整 key，例如 `robot/**` 会覆盖 `robot/arm/state`。Resource tree 共享这些表达式的路径前缀，Route cache 再按来源 routing region 和节点上下文记录可走的下一跳。

本文固定源码基线为 `eclipse-zenoh/zenoh@9fcd9cb5d364192c3e8a27e66de76f4bc750d1d5`；下文标出符号并直接摘录该提交的相关实现。

理解这两层后，Publisher 数据路径中的 `get_data_route()` 和 Query 路径中的 `get_query_route()` 就不再是黑盒。它们本质上都是：把字符串定位到 Resource，尝试命中缓存，必要时依据当前拓扑重新计算。

## Resource 是按 `/` 分段的表达式树

以下声明：

```text
robot/arm/state
robot/arm/cmd
robot/**
```

不会作为三个互不相关的字符串存入普通哈希表，而是共享前缀节点：

```text
root
└─ robot
   ├─ arm
   │  ├─ state
   │  └─ cmd
   └─ **
```

源码中的节点可压缩表示为：

```rust
struct Resource {
    parent: Option<Arc<Resource>>,
    expr: String,                         // 根到当前节点的完整表达式
    suffix: usize,                        // 当前 chunk 在 expr 中的起点
    nonwild_prefix: Option<Arc<Resource>>,
    children: SingleOrBoxHashSet<Child>,
    context: Option<Box<ResourceContext>>,
    face_contexts: IntHashMap<FaceId, Arc<FaceContext>>,
}

struct ResourceContext {
    matches: Vec<Weak<Resource>>,
    // 不同 routing region 的 data/query route caches
    // 以及跨 region 合并后的 data route
}
```

上面是为读者先看清角色写的教学压缩模型，下面才是固定提交的真实字段。它们分成两段摘录，是因为 context 类型在 `Resource` 之前定义：

```rust
pub(crate) struct ResourceContext {
    pub(crate) matches: Vec<Weak<Resource>>,
    pub(crate) hats: RegionMap<HatResourceContext>,
    pub(crate) data_routes: RwLock<DataRoutes>,
    #[cfg(feature = "stats")]
    pub(crate) stats_keys: zenoh_stats::StatsKeyCache,
}

pub(crate) struct HatResourceContext {
    pub(crate) ctx: Box<dyn Any + Send + Sync>,
    pub(crate) data_routes: RwLock<DataRoutes>,
    pub(crate) query_routes: RwLock<QueryRoutes>,
}
```

`matches` 是相交资源的弱引用列表；`hats` 是按 routing region 索引的 HAT 上下文。HAT 在这里是 Zenoh 按网络角色选择路由算法的实现层，例如 router、peer、client 或 broker；每个 region 可以有自己的路由角色。路由缓存分别有自己的 `RwLock`，只管 Resource 内的缓存，不等于保护整个拓扑的 Tables 锁。

`HatResourceContext.ctx` 用 `Box<dyn Any + Send + Sync>` 保存具体 HAT 的私有状态。`dyn Any` 是类型擦除的 trait object：Resource 不必在编译期依赖 router、peer、client 等每一种具体状态类型；HAT 使用时再 downcast 回自己的类型。`Box` 表示这个 context 由当前 `HatResourceContext` 独占拥有；`Send + Sync` 是跨线程传递与共享的类型约束，并不取代访问时需要的 Tables 同步。

router HAT 读取 Resource 状态时，通过 `res_hat` 把类型擦除的值还原为具体 `HatContext`；写入路径则需要可变借用：

对应的上游实现如下：
```rust
pub(self) fn res_hat<'r>(&self, res: &'r Resource) -> &'r HatContext {
    res.context().hats[self.region].ctx.downcast_ref().unwrap()
}

pub(self) fn res_hat_mut<'r>(&self, res: &'r mut Arc<Resource>) -> &'r mut HatContext {
    get_mut_unchecked(res).context_mut().hats[self.region]
        .ctx
        .downcast_mut()
        .unwrap()
}
```

`downcast_ref` 不复制 router context，只借出不可变引用；`downcast_mut` 经内部受控的 `get_mut_unchecked` 修改同一对象，调用者必须持有允许修改 Tables/Resource 的独占访问。若某 HAT 把另一种具体值放入槽位，downcast 的 `unwrap()` 会暴露内部类型不变量被破坏，而不是悄悄按另一种布局解释内存。

对应的上游实现如下：
```rust
pub struct Resource {
    pub(crate) parent: Option<Arc<Resource>>,
    pub(crate) expr: String,
    pub(crate) suffix: usize,
    pub(crate) nonwild_prefix: Option<Arc<Resource>>,
    pub(crate) children: SingleOrBoxHashSet<Child>,
    pub(crate) ctx: Option<Box<ResourceContext>>,
    pub(crate) face_ctxs: IntHashMap<FaceId, Arc<FaceContext>>,
}
```

这里 `parent` 与 `children` 都持有 `Arc<Resource>`，所以它们共同组成的父子强引用环不会靠引用计数自动归零。`matches` 则用 `Weak`，避免额外引入另一组强引用环。`ctx` 可缺省，树中间节点因此不必都承载声明与路由状态；后文的升级与清理代码负责建立、拆除这层状态。

`expr` 保留完整表达式，便于直接交给 key-expression 算法；`suffix` 又避免每次都重新扫描字符串寻找当前分段。`parent` 与 `children` 提供树形导航，`nonwild_prefix` 则快速定位通配符出现前的最长确定前缀。

`Resource::new` 会复制 parent 的完整 `String` 再追加这一段，所以每个节点都能独立产出完整 key expression，不必沿 parent 链重拼；代价是声明/创建时复制前缀并为各节点保存字符串。`suffix` 存的是当前分段在完整表达式中的起始字节位置，而不是另一份 `String`。这把分配放在低频资源建立时，换取路由时可以按节点直接取表达式片段。

只有实际承载声明或路由上下文的终点需要 `ResourceContext`。`ctx: Option<Box<ResourceContext>>` 让中间节点只保存一个可空的 Box 指针，而不是把较大的 context 内嵌到每个 Resource；Box 是该节点对 context 的唯一所有者，`close()` 中 `ctx.take()` 会拆掉这项拥有关系。`face_ctxs` 则放 `Arc<FaceContext>`，因为该 Face 关联状态会被多处共享。这两种字段分别对应“独占拥有”和“共享拥有”，不可把 Box 与 Arc 当成同一种智能指针。

`children` 的 key 不是完整表达式，而是当前层的分段。例如 `robot/arm/state` 的 `robot` 节点只用 `arm` 查下一层。固定实现让 `Child` 以子节点 `suffix()` 的字符串做相等比较与哈希，并实现 `Borrow<str>`，所以查找 `"arm"` 不必先创建一个临时 Resource：

```rust
#[derive(Clone)]
pub(crate) struct Child(Arc<Resource>);

impl Deref for Child {
    type Target = Arc<Resource>;

    fn deref(&self) -> &Self::Target {
        &self.0
    }
}

impl DerefMut for Child {
    fn deref_mut(&mut self) -> &mut Self::Target {
        &mut self.0
    }
}

impl PartialEq for Child {
    fn eq(&self, other: &Self) -> bool {
        self.0.suffix() == other.0.suffix()
    }
}

impl Eq for Child {}

impl Hash for Child {
    fn hash<H: Hasher>(&self, state: &mut H) {
        self.0.suffix().hash(state);
    }
}

impl Borrow<str> for Child {
    fn borrow(&self) -> &str {
        self.0.suffix()
    }
}
```

节点分支度也不均匀：大量路径节点没有孩子，许多只有一个。若每个 `children` 都直接持有普通 HashSet，空集合和单孩子节点也要承担哈希表表示。Zenoh 的 `SingleOrBoxHashSet` 用枚举把这三种规模分开：

对应的上游实现如下：
```rust
pub enum SingleOrBoxHashSet<T> {
    Empty,
    Single(T),
    Set(Box<ahash::HashSet<T>>),
}

impl<T> SingleOrBoxHashSet<T>
where
    T: Eq + Hash,
{
    #[inline]
    pub fn new() -> Self {
        SingleOrBoxHashSet::Empty
    }

    #[inline]
    pub fn insert(&mut self, v: T) -> bool {
        match self {
            SingleOrBoxHashSet::Empty => {
                *self = SingleOrBoxHashSet::Single(v);
                true
            }
            SingleOrBoxHashSet::Single(single) => {
                if *single == v {
                    *self = SingleOrBoxHashSet::Single(v);
                    false
                } else {
                    let mut swap = SingleOrBoxHashSet::Set(Box::default());
                    std::mem::swap(self, &mut swap);
                    if let SingleOrBoxHashSet::Set(set) = self {
                        if let SingleOrBoxHashSet::Single(single) = swap {
                            set.insert(single);
                            set.insert(v);
                            return true;
                        }
                    }
                    unreachable!()
                }
            }
            SingleOrBoxHashSet::Set(set) => {
                if set.is_empty() {
                    *self = SingleOrBoxHashSet::Single(v);
                    true
                } else {
                    set.insert(v)
                }
            }
        }
    }

    pub fn contains<Q>(&self, v: &Q) -> bool
    where
        T: Borrow<Q>,
        Q: Hash + Eq + ?Sized,
    {
        match self {
            SingleOrBoxHashSet::Empty => false,
            SingleOrBoxHashSet::Single(single) => Borrow::<Q>::borrow(single) == v,
            SingleOrBoxHashSet::Set(set) => set.contains(v),
        }
    }

    #[inline]
    pub fn get<Q>(&self, v: &Q) -> Option<&T>
    where
        T: Borrow<Q>,
        Q: Hash + Eq + ?Sized,
    {
        match self {
            SingleOrBoxHashSet::Empty => None,
            SingleOrBoxHashSet::Single(single) => {
                (Borrow::<Q>::borrow(single) == v).then_some(single)
            }
            SingleOrBoxHashSet::Set(set) => set.get(v),
        }
    }
}
```

第一次插入时只存 `Single(Child)`，第二个不同 child 才把两者提升为装在 Box 内的 ahash HashSet；因此常见的空/单分支节点不需要分配哈希桶。单 child 查询直接比较借用的 suffix，分支变宽后才走哈希查找。代价是枚举分支和“从 one 到 many”的转换逻辑；高分支节点仍要持有 hash set 及其 bucket 存储，不能把它说成全树都零分配。

固定源码来源：`eclipse-zenoh/zenoh@9fcd9cb5d364192c3e8a27e66de76f4bc750d1d5` 中的 `Resource`、`ResourceContext`、`HatResourceContext`。

## `Arc`、`Weak` 与树所有权各司其职

多个路由和 Face context 会共享节点，所以结构使用 `Arc<Resource>`。需要特别纠正一个常见误读：这个固定提交的树父子关系本身是双向强引用——`Resource.parent` 是 `Option<Arc<Resource>>`，children 中的 `Child` 也包着 `Arc<Resource>`。因此父子链形成的强引用环不会靠 Arc 自行归零，必须由 Resource 清理逻辑显式拆开。与此不同，匹配关系是非拥有关系，使用 `Weak<Resource>`：

```text
parent --Arc--> child --Arc--> parent
match A --Weak--> match B
match B --Weak--> match A
```

`Weak` 可以通过 `upgrade()` 暂时尝试取得 `Arc`。节点已被回收时，升级返回 `None`，不会访问悬空指针。它适合表达“我知道这个对象，但不负责让它存活”。

`Arc<T>` 在 `T` 满足线程安全 trait bound 时，是跨线程可共享的强所有权句柄：clone 一个 Arc 会增加同一分配块中的原子强引用计数，不会深拷贝 `T`；最后一个强 Arc drop 后，Resource 才能析构。`Arc::downgrade` 产生的 Weak 不增加强计数，只有在目标尚活着时 `upgrade()` 才会临时拿到强 Arc。原子引用计数只管分配块寿命，不会自动让 Resource 字段线程安全；读写树和 context 仍由 Tables 的 `RwLock` 排斥。

`Child` 的强 Arc 包装和基于 suffix 的 key 已在前面看到；新节点创建时会反向强持有 parent，并继承/更新确定前缀：

对应的上游实现如下：
```rust
fn new(parent: &Arc<Resource>, suffix: &str, context: Option<ResourceContext>) -> Resource {
    let nonwild_prefix = match &parent.nonwild_prefix {
        None => {
            if suffix.contains('*') {
                Some(parent.clone())
            } else {
                None
            }
        }
        Some(prefix) => Some(prefix.clone()),
    };

    Resource {
        parent: Some(parent.clone()),
        expr: parent.expr.clone() + suffix,
        suffix: parent.expr.len(),
        nonwild_prefix,
        children: SingleOrBoxHashSet::new(),
        ctx: context.map(Box::new),
        face_ctxs: IntHashMap::new(),
    }
}
```

匹配边尤其适合弱引用，因为 `a/*` 与 `a/b` 的相交关系是对称的；若双方互持 `Arc`，两者即使都没有声明，也会额外互相阻止回收。弱边可在 `upgrade()` 成功时临时访问目标，但不负责让目标常驻。树的 parent/child 强关系则由固定提交 `eclipse-zenoh/zenoh@9fcd9cb5d364192c3e8a27e66de76f4bc750d1d5` 中的 `Resource::clean` 与 `Resource::close` 负责拆环。

具体反例是机器人切换 topic 命名后不断 declare/undeclare：若只从业务索引删掉叶节点、忘了从父节点 `children` 移除它，父子 Arc 环仍在，RSS 会随重复改名逐次上涨。Zenoh 的增量清理只删无 children 且强引用计数不超过预期临时引用数的叶节点，再从 parent.children 摘除并向上清理；全局 close 则 drain 子节点并清空 parent/context。它让读路径的树导航简单，但把正确性负担放到显式清理代码。

## `make_resource` 迭代创建节点

Resource 创建过程沿 `/` 逐段推进：

```text
make_resource("robot/arm/state")
  current = root
  chunk "robot": children 中存在则复用，否则创建
  chunk "arm":   children 中存在则复用，否则创建
  chunk "state": children 中存在则复用，否则创建
  terminal: 若没有 ResourceContext，则升级为声明终点
```

实现刻意采用迭代而非递归。这样树深度由输入表达式决定时，不会把同样深度转化成调用栈深度，也更容易在循环中维护当前完整表达式和 suffix。

```rust
        let mut from = from.clone();
        // do not use recursion as the tree may have arbitrary depth
        while let Some((chunk, rest)) = Self::split_first_chunk(suffix) {
            if let Some(child) = get_mut_unchecked(&mut from).children.get(chunk) {
                from = child.0.clone();
            } else {
                let new = Arc::new(Resource::new(&from, chunk, None));
                if rest.is_empty() {
                    tracing::debug!("Register resource {}", new.expr());
                }
                get_mut_unchecked(&mut from)
                    .children
                    .insert(Child(new.clone()));
                from = new;
            };
            suffix = rest;
        }
        let hat = tables
            .hats
            .map_ref(|d| HatResourceContext::new(d.new_resource()));
        Resource::upgrade_resource(&mut from, hat);
        from
```

调用者此时正建立或解析声明，对 `Tables` 的可变借用在本函数内用于访问 region 的 HAT 工厂；每轮沿当前节点的 `children` 复用或创建一个分段节点，插入父节点的强 `Child` 后再把局部 `Arc` 移到新节点。循环结束才给终点补上 context。下一步由声明路径计算匹配集合并写入两端的弱边，见下方 `match_resource`。

精确表达式的树查找成本为 `O(D)`，`D` 是 `/` 分段数；每一层通常执行一次 children 哈希查找。它不是按字符串字符数逐个遍历所有声明。

`make_resource` 的固定实现是 `eclipse-zenoh/zenoh@9fcd9cb5d364192c3e8a27e66de76f4bc750d1d5` 中的 `Resource::make_resource`。

## `matches` 把昂贵的相交判断前移到声明期

Key expression 不是简单相等关系。例如：

```text
a/*       intersects a/b
a/**      intersects a/b/c
a/b       does not intersect x/b
```

`match_resource` 在声明或资源建立时计算相交集合，并维护双向弱引用边：

```text
Resource("a/*")  --Weak--> Resource("a/b")
Resource("a/b")  --Weak--> Resource("a/*")
```

这样路由失效时，无需重新扫描整个资源树来寻找“哪些缓存可能受本次声明变化影响”，而是从当前节点沿 `matches` 直接触达相关节点。

这是典型的读写权衡：

- 声明路径更贵，需要计算相交关系并维护边；
- 高频数据路径更便宜，可以直接使用预计算关系与 route cache；
- 通配符越宽，匹配边越多，内存和失效扇出越大。

`get_matches` 与 `match_resource` 由固定提交分别承担“找出交集”和“安装交集关系”。前者在声明期工作；若把这次表达式相交遍历放到每条 20 Hz 状态消息上，key 数量越多，消费线程就越常重走资源树。它把待检查状态放进 FIFO `VecDeque`，每项保存“尚未匹配的表达式后缀”和“当前 Resource”，循环弹出而不递归，因此超长 key 不会变成同等深度的 Rust 调用栈。

```rust
pub fn get_matches(tables: &TablesData, key_expr: &keyexpr) -> Vec<Weak<Resource>> {
    pub fn visit_nodes<T>(node: T, mut visit: impl FnMut(T, &mut VecDeque<T>)) {
        let mut nodes = VecDeque::from([node]);
        while let Some(node) = nodes.pop_front() {
            visit(node, &mut nodes);
        }
    }
    fn get_matches_from(
        key_expr: &keyexpr,
        from: &Arc<Resource>,
        matches: &mut Vec<Weak<Resource>>,
    ) {
        visit_nodes((key_expr, from), |(key_expr, from), nodes| {
            if from.parent.is_none() || from.suffix() == "/" {
                for child in from.children.iter() {
                    nodes.push_back((key_expr, child));
                }
                return;
            }
            let suffix: &keyexpr = from
                .suffix()
                .strip_prefix('/')
                .unwrap_or(from.suffix())
                .try_into()
                .unwrap();
            let (ke_chunk, ke_rest) = match key_expr.split_once('/') {
                // SAFETY: chunks of keyexpr are valid keyexprs
                Some((chunk, rest)) => unsafe {
                    (
                        keyexpr::from_str_unchecked(chunk),
                        Some(keyexpr::from_str_unchecked(rest)),
                    )
                },
                None => (key_expr, None),
            };
            let ke_chunk_intersects_suffix = ke_chunk.intersects(suffix);
            let ke_chunk_is_wild = ke_chunk.as_bytes() == b"**";
            let suffix_is_wild = suffix.as_bytes() == b"**";
            match ke_rest {
                None => {
                    if ke_chunk_intersects_suffix {
                        if from.ctx.is_some() {
                            matches.push(Arc::downgrade(from));
                        }
                        if let Some(child) =
                            from.children.get("/**").or_else(|| from.children.get("**"))
                        {
                            if child.ctx.is_some() {
                                matches.push(Arc::downgrade(child))
                            }
                        }
                    }
                    if (ke_chunk_is_wild && ke_chunk_intersects_suffix) || suffix_is_wild {
                        for child in from.children.iter() {
                            nodes.push_back((key_expr, child));
                        }
                    }
                }
                Some(rest) => {
                    if ke_chunk_intersects_suffix
                        && rest.as_bytes() == b"**"
                        && from.ctx.is_some()
                    {
                        matches.push(Arc::downgrade(from));
                    }
                    for child in from.children.iter() {
                        if (ke_chunk_is_wild && ke_chunk_intersects_suffix) || suffix_is_wild {
                            nodes.push_back((key_expr, child));
                        } else if ke_chunk_intersects_suffix {
                            nodes.push_back((rest, child));
                        }
                    }
                    if (suffix_is_wild && ke_chunk_intersects_suffix) || ke_chunk_is_wild {
                        nodes.push_back((rest, from));
                    }
                }
            };
        })
    }
    let mut matches = Vec::new();
    get_matches_from(key_expr, &tables.root_res, &mut matches);
    matches.sort_unstable_by_key(Weak::as_ptr);
    matches.dedup_by_key(|res| Weak::as_ptr(res));
    matches
}
```

每次循环从 Resource 的 `suffix()` 取当前树段，与待匹配 key expression 的第一段做 `intersects`；只有可能相交时才把后续 `(rest, child)` 状态压回队列。`*`/`**` 让遍历分支变宽，所以一个宽通配订阅可能访问许多节点；只有带 `ctx` 的声明终点会进入返回列表。相交路径可能重复抵达同一节点，最后按 Weak 指针排序并去重。`from_str_unchecked` 绕过解析器，但它处理的是已验证 key expression 切出来的段，源码注释明确依赖“chunks of keyexpr are valid keyexprs”；不能把这个写法照搬到未经验证的外部字符串。结果仍是 Weak，因为“声明时需要找到它”和“永远拥有它”是两件事。

`VecDeque` 与结果 `Vec` 都按实际遍历/匹配数增长，没有固定上限。一个 `robot/**` 订阅会让声明阶段的工作队列和 matches 列表随匹配树规模变大；这项分配发生在拓扑更新期，换来的则是后续样本不用逐条重新扫描所有声明。

这里要区分“计算匹配集合”和“安装双向边”：`get_matches` 负责计算，`match_resource` 接收已经得到的 `Weak<Resource>` 列表并维护对称关系。固定实现的边界如下：

```rust
    pub fn match_resource(
        _tables: &TablesData,
        res: &mut Arc<Resource>,
        matches: Vec<Weak<Resource>>,
    ) {
        if res.ctx.is_some() {
            for match_ in &matches {
                let mut match_ = match_.upgrade().unwrap();
                get_mut_unchecked(&mut match_)
                    .context_mut()
                    .matches
                    .push(Arc::downgrade(res));
            }
            get_mut_unchecked(res).context_mut().matches = matches;
        } else {
            tracing::error!("Call match_resource() on context less res {}", res.expr());
        }
    }

    pub fn upgrade_resource(res: &mut Arc<Resource>, hat: RegionMap<HatResourceContext>) {
        if res.ctx.is_none() {
            get_mut_unchecked(res).ctx = Some(Box::new(ResourceContext::new(hat)));
        }
    }
```

新声明端先取得 `matches`（弱引用），再把自身的弱引用加入每个对端列表，然后替换自己的列表，形成双向索引。`upgrade_resource` 则只在首次需要时创建 context。这里的 `upgrade().unwrap()` 依赖 Tables 写侧串行维护匹配边的内部不变量；它不是可忽略错误处理的通用 Weak 用法。

## Route cache 的映射键包含来源区域与映射后的节点

同一个 key expression 从不同来源进入，允许转发的方向可能不同。这里 Region 是拓扑划分出的路由域，NodeId 是 HAT 拓扑中的路由上下文编号，不是机器人 ID。原因包括拓扑防环、路由角色差异、来源 Face 过滤和区域策略。因此缓存不能只用 resource 作为 key。

`Routes<T>` 并不是全局拿 key expression 做哈希索引。每个 Resource context 自己拥有一个或多个 `Routes<T>`；在其中，逻辑映射键是：

```text
Resource context selects Routes object
Routes mapping key: (source Region, mapped source NodeId)
route is usable only if Routes.version == current routes_version
```

固定源码使用 `RegionMap<NodeIdMap<T>>`，而 `NodeIdMap<T>` 是 `Vec<Option<T>>`。先用来源 Region 取到区域向量，再以 `NodeId as usize` 直接索引槽位；新 NodeId 会通过 `resize_with` 补齐中间空槽。这样免去节点 ID 的第二次哈希查找，但如果 NodeId 很大而实际节点很少，向量扩容及空槽会占空间。`NodeId` 在该实现中是 `u16`。

缓存值通常是 `Arc<Route>`。命中时只需克隆 `Arc`，不复制目的地集合。Route 构造完成后按不可变对象使用，因此多个发送线程可以安全共享同一快照。

## 版本号让全局失效保持常数成本

每一个 `Routes<T>` 都保存当前版本。全局或区域级拓扑变化时，调用方传入新的版本；只要不相等，旧映射就不会被命中。固定提交 `eclipse-zenoh/zenoh@9fcd9cb5d364192c3e8a27e66de76f4bc750d1d5` 中的 `Routes::{get_route,set_route}` 如下：

```rust
pub type RoutesVersion = u64;

pub(crate) struct Routes<T> {
    mapping: RegionMap<NodeIdMap<T>>,
    version: u64,
}

pub(crate) type NodeIdMap<T> = Vec<Option<T>>;

impl<T> Default for Routes<T> {
    fn default() -> Self {
        Self {
            mapping: RegionMap::default(),
            version: 0,
        }
    }
}

impl<T> Routes<T> {
    pub(crate) fn clear(&mut self) {
        self.mapping.clear();
    }

    pub(crate) fn get_route(
        &self,
        version: RoutesVersion,
        region: &Region,
        node_id: NodeId,
    ) -> Option<&T> {
        if version != self.version {
            return None;
        }
        self.mapping
            .get(region)
            .and_then(|rs| rs.get(node_id as usize))
            .and_then(|r| r.as_ref())
    }

    pub(crate) fn set_route(
        &mut self,
        version: RoutesVersion,
        region: &Region,
        node_id: NodeId,
        route: T,
    ) {
        if self.version != version {
            self.clear();
            self.version = version;
        }
        let aux = |routes: &mut NodeIdMap<T>| {
            routes.resize_with(node_id as usize + 1, || None);
            routes[node_id as usize] = Some(route);
        };
        if let Some(routes) = self.mapping.get_mut(region) {
            aux(routes);
        } else {
            let mut routes = NodeIdMap::default();
            aux(&mut routes);
            self.mapping.insert(*region, routes);
        }
    }
}
```

`get_route` 只对照版本并索引，不会在读路径上清表。等某次 miss 成功计算并调用 `set_route`，发现版本不同时才清空这份 `Routes` 的所有 region/node 项、记下新版本，再插入当前 route。这样失效操作不必遍历整个 Resource tree；旧项占用的内存会留到对应缓存下次写入时才释放。

查找流程如下：

```text
get_or_set_route(current_version, region, node_id)
  |
  +─ cache.version != current_version -> MISS
  |
  +─ mapping[region][node_id] 存在 -> HIT，clone Arc<Route>
  |
  └─ MISS
       1. 获取这份 Resource cache 的写锁
       2. 再检查一次是否已有其他线程填入
       3. 仍 miss：持写锁计算 route
       4. 若缓存版本变化，清空映射并更新 version
       5. 写入新 route
```

这是双重检查，但不能据此推断 miss 计算可以并行。首轮 `read()` miss 后读 guard 释放，线程才争用写锁；持锁者再次检查，若仍 miss，就在写 guard 尚存活时调用计算闭包并写入。其他线程可以同时在首轮发现 miss，却会阻塞在写锁获取处；等首个计算和插入结束后，它们才进入第二次检查并复用结果。这样避免重复 route 构造，却使一次慢 miss 暂时挡住同一 Resource cache 的读命中和其他 miss。

这里要区分两把锁。`route_data` 外层持有 `TablesLock.tables` 读 guard，确保一次计算看到一致的拓扑；`Routes` 自己的 `std::sync::RwLock` 写 guard 只串行化该 Resource 的缓存。`compute_route()` 在第二把锁内运行，却没有持有 Tables 写锁。读写锁是同步锁而非 async mutex：调用它的线程不能通过 `.await` 让出执行权；标准库实现可以短暂自旋或进入阻塞等待，若线程被阻塞，OS 调度器可转而运行其他 runnable 线程。拿锁的线程也不因此独占 CPU。

合并 data route cache 的 miss 还有一层工作：`get_data_route` 闭包遍历各 routing region，再读取或填充对应的 per-Hat cache 并合并方向。因此合并缓存的写 guard 在整个跨 region 汇总期间都存活；这份 Resource cache 的冷 miss 延迟可能包含多份 per-Hat cache 查询或计算。不同 Resource 有独立的 cache lock，争用范围不会因为同一把缓存锁扩展到整棵 Resource tree；但它们仍共享 Tables 读锁保护的拓扑快照。

对应的上游实现如下：
```rust
pub(crate) fn get_or_set_route<T: Clone>(
    routes: &RwLock<Routes<T>>,
    version: RoutesVersion,
    region: &Region,
    node_id: NodeId,
    compute_route: impl FnOnce() -> T,
) -> T {
    if let Some(route) = routes.read().unwrap().get_route(version, region, node_id) {
        return route.clone();
    }
    let mut routes = routes.write().unwrap();
    // NOTE(regions): we supposedly re-read the routes here because they might've changed, but I'm
    // not sure this is true given that all callers would've acquired `TablesLock::tables`.
    if let Some(route) = routes.get_route(version, region, node_id) {
        return route.clone();
    }
    let route = compute_route();
    routes.set_route(version, region, node_id, route.clone());
    route
}
```

具体反例是网关刚接受新订阅后，同一个 `robot/arm/state` Resource 上同时收到多路冷启动样本：首条样本要扫描相交声明与目标 Face，其他线程在 cache 写锁前排队。若这次计算检查数千个匹配项，原本能命中的其他样本也会暂时等在这把锁上，出口队列入队时间拉长，控制端读到的状态年龄上升。设计节省重复构造并保证缓存更新顺序，代价是按 Resource 串行化 miss。最小复刻若把计算移到写锁外，会更容易并行，却必须接受重复运算，并处理拓扑版本在计算期间改变的问题。

## 局部失效与全局失效解决不同规模的变化

声明变更只影响与某个表达式相交的资源，适合局部失效：

```text
declare subscriber("a/**")
  -> clear resource("a/**") data route cache
  -> 遍历 matches
       clear resource("a/*") data route cache
       clear resource("a/b") data route cache
       保留 resource("x/**") data route cache
```

Face、拓扑或 routing region 发生变化时，潜在影响接近全局。逐个遍历所有 Resource 清缓存的成本很高，于是系统只递增版本号：

```text
HatTablesData.routes_version += 1
TablesData.routes_version    += 1
```

旧缓存仍留在原处，但下一次访问会看到版本不一致，然后惰性清空并重算。全局失效动作本身接近 `O(1)`，代价被分摊到后续真实访问的资源上。

两种策略组合后形成清晰边界：已知受影响集合较小时精确清除；影响范围难以枚举时推进 epoch/version。

固定提交 `eclipse-zenoh/zenoh@9fcd9cb5d364192c3e8a27e66de76f4bc750d1d5` 中的 `Hat::disable_data_routes` 展示局部失效会同时清目标 Resource 与其相交资源的 per-Hat、合并 data route 缓存：

```rust
fn disable_data_routes(&mut self, res: &mut Arc<Resource>) {
    if res.ctx.is_some() {
        get_mut_unchecked(res).context_mut().hats[self.region()].disable_data_routes();
        get_mut_unchecked(res).context_mut().disable_data_routes();

        for match_ in &res.context().matches {
            let mut match_ = match_.upgrade().unwrap();
            if !Arc::ptr_eq(&match_, res) {
                get_mut_unchecked(&mut match_).context_mut().hats[self.region()]
                    .disable_data_routes();
                get_mut_unchecked(&mut match_)
                    .context_mut()
                    .disable_data_routes();
            }
        }
    }
}
```

例如新增 `robot/**` Subscriber 后，既有 `robot/arm/state` 的 route 可能要把新 Face 加入；只清新声明自身缓存不够，因为旧 Resource 的 `matches` 里也与它相交。源码从匹配列表 `upgrade()` 每一条 Weak edge，再清对应两个缓存。失败遗漏的可观察结果是新 Subscriber 声明成功、控制台没有错误，但它持续收不到已存在 key 上的样本，直到其他拓扑变化碰巧触发重算。过度清除则会让无关 key 冷启动重新计算。

拓扑级失效则在 HAT 状态上递增 HAT 自己的 `routes_version`，并递增全局 `TablesData.routes_version`；固定来源是 `Hat::disable_all_routes` 与 `TablesData::disable_all_routes`。版本递增用 `saturating_add(1)`，不遍历整个 Resource tree。

```rust
fn disable_all_routes(&mut self, tables: &mut TablesData) {
    let routes_version = &mut tables.hats[self.region()].routes_version;
    *routes_version = routes_version.saturating_add(1);

    tables.disable_all_routes();
}
```

对应的上游实现如下：
```rust
pub(crate) fn disable_all_routes(&mut self) {
    let routes_version = &mut self.routes_version;
    *routes_version = routes_version.saturating_add(1);
}
```

## 资源清理不能只看“没有声明”

一个 Resource 即使不再直接承载声明，也可能仍被子节点、Face mapping、Route 或临时计算引用。下面固定提交中的 `Resource::clean` 会 clone 当前 Arc，再以受控的内部可变访问检查强引用计数；当它不是 root、没有孩子且引用数不超过当前调用预期阈值时，删掉匹配边、断开 nonwild prefix、从 parent.children 摘除自己，然后递归尝试清父节点。

```rust
pub fn clean(res: &mut Arc<Resource>) {
    let mut resclone = res.clone();
    let mutres = get_mut_unchecked(&mut resclone);
    if let Some(ref mut parent) = mutres.parent {
        tracing::trace!(strong_count = Arc::strong_count(res));
        if Arc::strong_count(res) <= 3 && res.children.is_empty() {
            // consider only childless resource held by only one external object (+ 1 strong count for resclone, + 1 strong count for res.parent to a total of 3 )
            tracing::debug!("Unregister resource {}", res.expr());
            if let Some(context) = mutres.ctx.as_mut() {
                for match_ in &mut context.matches {
                    let mut match_ = match_.upgrade().unwrap();
                    if !Arc::ptr_eq(&match_, res) {
                        let mutmatch = get_mut_unchecked(&mut match_);
                        if let Some(ctx) = mutmatch.ctx.as_mut() {
                            ctx.matches
                                .retain(|x| !Arc::ptr_eq(&x.upgrade().unwrap(), res));
                        }
                    }
                }
            }
            mutres.nonwild_prefix.take();
            {
                get_mut_unchecked(parent).children.remove(res.suffix());
            }
            Resource::clean(parent);
        }
    }
}

pub fn close(self: &mut Arc<Resource>) {
    let r = get_mut_unchecked(self);
    for mut c in r.children.drain() {
        Self::close(&mut c);
    }
    r.parent.take();
    r.nonwild_prefix.take();
    r.ctx.take();
    r.face_ctxs.clear();
}
```

删除前，它从所有匹配节点的 `matches` 中移除自己，再从父节点 children 删除，随后向上尝试清理已经变空的祖先。

固定版本中使用 `Arc::strong_count <= 3` 判断当前是否只剩预期引用。这个数字与调用现场持有的临时 clone 数耦合，不是通用的所有权证明。修改清理代码时若多保留一个局部 `Arc`，就可能让节点永远达不到阈值；若错误减少计数假设，又可能从树索引中提前移除仍在使用的节点。

Runtime 强制关闭时不再依赖普通增量清理，而由 `root_res.close()` 深度 drain children、断开 parent/nonwild 引用并清空 contexts。它是完整 teardown 路径。下面从 Runtime 的调用点确认关闭顺序和 Tables 锁边界：

对应的上游实现如下：
```rust
impl Closee for Arc<RuntimeState> {
    type CloseArgs = ();
    async fn close_inner(&self, _: ()) {
        tracing::trace!("Runtime::close()");
        // TODO: Plugins should be stopped
        // TODO: Check this whether is able to terminate all spawned task by Runtime::spawn
        self.task_controller.terminate_all_async().await;
        self.manager.close().await;
        // clean up to break cyclic reference of self.state to itself
        self.transport_handlers.write().unwrap().clear();
        // TODO: the call below is needed to prevent intermittent leak
        // due to not freed resource Arc, that apparently happens because
        // the task responsible for resource clean up was aborted earlier than expected.
        // This should be resolved by identifying corresponding task, and placing
        // cancellation token manually inside it.
        let mut tables = self.router.tables.tables.write().unwrap();
        tables.data.root_res.close();
        tables.data.faces.clear();
    }
}
```

关闭先等待 Runtime 管理的任务结束，再关闭 manager、清 transport handler，最后取得 Tables 写锁并递归断开 Resource 树，之后清 Face 表。固定源码注释还指出，资源清理任务提前 abort 曾造成偶发资源泄漏，因此强制关闭需要这条全树 teardown 路径；不能只依赖 `clean` 在日常 undeclare 中逐叶回收。

`clean` 本身没有取得 Tables 锁；真正的排他边界在调用现场。先看 Face 怎样解析待撤销的表达式：已有资源的查找先拿 Tables 读锁，找到节点后明确释放读 guard，随后取得写锁，并在 guard 存活期间把 `&mut Tables` 交给闭包。

对应的上游实现如下：
```rust
pub(crate) fn with_mapped_nullable_expr<F>(
    &self,
    expr: &WireExpr<'_>,
    make_if_unknown: bool,
    mut f: F,
) where
    F: FnMut(&mut Tables, Option<Arc<Resource>>),
{
    let (res, mut wtables) = if !expr.is_empty() {
        let rtables = self.tables.tables.read().unwrap();

        let Some(mut prefix) = rtables
            .data
            .get_mapping(&self.state, &expr.scope, expr.mapping)
            .cloned()
        else {
            tracing::error!(?expr.scope, ?expr.mapping, "Unknown wire expr");
            return;
        };

        if let Some(res) = Resource::get_resource(&prefix, &expr.suffix) {
            drop(rtables);
            (Some(res), self.tables.tables.write().unwrap())
        } else if make_if_unknown {
            let mut fullexpr = prefix.expr().to_string();
            fullexpr.push_str(expr.suffix.as_ref());
            let mut matches = keyexpr::new(fullexpr.as_str())
                .map(|ke| Resource::get_matches(&rtables.data, ke))
                .unwrap_or_default();
            drop(rtables);
            let mut wtables = self.tables.tables.write().unwrap();
            let mut res =
                Resource::make_resource(&mut wtables, &mut prefix, expr.suffix.as_ref());
            matches.push(Arc::downgrade(&res));
            Resource::match_resource(&wtables.data, &mut res, matches);
            (Some(res), wtables)
        } else {
            tracing::error!(?prefix, suffix = ?expr.suffix, "Unknown resource");
            return;
        }
    } else {
        (None, self.tables.tables.write().unwrap())
    };

    tracing::debug!(?expr, expr.mapped = ?res);

    let tables = &mut *wtables;

    f(tables, res)
}
```

`with_mapped_nullable_expr` 的闭包类型要求 `&mut Tables`，局部变量 `wtables` 则持有写 guard。摘录还包含 `make_if_unknown` 分支：若允许创建缺少的 Resource，它会在释放读 guard 后取得写 guard，再创建节点并安装匹配边；本节的撤销调用传 `false`，只接受已有资源。下面的调用者在该闭包里撤销 Subscriber；当最后一方离开时，路由缓存失效后才调用 `Resource::clean`：

对应的上游实现如下：
```rust
self.with_mapped_nullable_expr(expr, /* make_if_unknown */ false, |tables, res| {
    let region = self.state.region;

    let mut ctx = DispatcherContext {
        tables_lock: &self.tables,
        tables: &mut tables.data,
        src_face: &mut self.state.clone(),
        send_declare,
    };

    if let Some(mut res) =
        tables.hats[region].unregister_subscriber(ctx.reborrow(), id, res.clone(), node_id)
    {
        tables.hats[region].disable_data_routes(&mut res);

        let mut remaining = tables
            .hats
            .values_mut()
            .filter(|hat| hat.remote_subscribers_of(ctx.tables, &res).is_some())
            .collect_vec();

        if (*remaining).is_empty() {
            for hat in tables.hats.values_mut() {
                hat.unpropagate_subscriber(ctx.reborrow(), res.clone());
            }
            Resource::clean(&mut res);
        } else if let [last_owner] = &mut *remaining {
            last_owner.unpropagate_last_non_owned_subscriber(ctx, res.clone())
        }
    }
});
```

调用链因此是：先从 Tables 读锁下定位 Resource，再转入 Tables 写锁；写侧更新 HAT 声明和缓存，最后清理树节点。`get_mut_unchecked` 不能脱离这个写侧调用约束单独复用：若并发路由仍在读同一 Resource，或另一路更新绕过 Tables 写锁，改动会破坏 Rust 的别名不变量。锁排斥经 Tables 访问路径发生的并发读写；它不会替代 `Arc` 计数阈值，也不会自动回收未从父子索引摘除的节点。

## 数据结构与性能模型

设资源节点数为 `R`，表达式平均深度为 `D`，当前节点的相交资源数为 `M`，目的 Face 数为 `F`：

| 操作 | 典型复杂度 | 容易被忽略的成本 |
|---|---:|---|
| 精确 resource 查找/创建 | `O(D)` | 空/单 child 走枚举分支，多 child 走哈希；创建仍复制完整前缀字符串 |
| 建立匹配集合 | 与候选资源数相关 | keyexpr 相交算法、去重、双向 Weak 边 |
| route cache hit | 近似 `O(1)` | `RwLock` 读、RegionMap、Vec 索引、Arc clone |
| route cache miss | 取决于 Hat 算法，常含 `O(M + F)` | 目的 Face 去重、写锁与 Route 分配 |
| 局部失效 | `O(M)` | 清自身与全部相交资源缓存 |
| 全局失效 | `O(1)` | 后续首次访问承担重算抖动 |

通配符密度是重要自变量。大量 `/**` 表达式会同时扩大匹配图边数、局部失效扇出和路由候选集合。只测试 payload 吞吐，无法暴露这个瓶颈；性能评估应同时改变资源数、平均深度、通配符比例、Face 数和拓扑变更频率。

## 最小复刻的四个阶段

1. 先实现按 `/` 分段的树，只支持精确 key；验证创建、查找和叶节点回收。
2. 加入 `*`、`**` 相交判断，并用非拥有边保存双向 matches；验证删除任意一端后没有悬挂访问。
3. 为每个 Resource 增加不可变 `Arc<Route>` 缓存；读 miss 后获取写锁、二次查找，并在写 guard 内计算和插入。
4. 最后加入局部 matches 失效和全局 version 失效；统计实际 route compute 次数，证明 hit 与 invalidation 确实发生。

Resource tree 的价值不是把字符串换成树，而是把声明期建立的结构、数据期复用的 Route 和拓扑变化时的失效协议组合成一个整体。缺少任何一部分，缓存都可能返回已经过时的目的地集合。
