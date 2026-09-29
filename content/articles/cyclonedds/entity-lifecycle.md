# Entity 生命周期：handle、parent/child、pin/ref 为什么要同时存在

固定源码：e54e991f75a3e67f8e628da3171122e36ea5b872。

## 一个整数 handle 背后为什么需要复杂生命周期

DDS C API 暴露 dds_entity_t。若它只是裸指针索引，很快会出现：

~~~text
Thread A                  Thread B
lookup writer handle
得到 pointer
                          dds_delete(writer)
                          free(pointer)
继续访问 pointer
=> use-after-free
~~~

Cyclone DDS 的 Entity 层就是在避免这种竞态。

## Entity tree

dds_entity_init() 有一个重要不变量：

~~~c
assert(
  (kind == DDS_KIND_CYCLONEDDS) ==
  (parent == NULL));
~~~

只有根对象没有 parent。常规层级近似为：

~~~text
CycloneDDS
└─ Domain
   └─ Participant
      ├─ Publisher
      │  └─ Writer
      ├─ Subscriber
      │  └─ Reader
      ├─ Topic
      └─ WaitSet
~~~

## children 为什么是 AVL tree

注册 child 时：

~~~c
void dds_entity_register_child(
  dds_entity *parent,
  dds_entity *child)
{
  assert(
    ddsrt_avl_lookup(
      &dds_entity_children_td,
      &parent->m_children,
      &child->m_iid) == NULL);

  ddsrt_avl_insert(
    &dds_entity_children_td,
    &parent->m_children,
    child);

  dds_entity_add_ref_locked(parent);
}
~~~

这不是“C 没有 vector 才用树”。访问模式本身要求动态插入/删除、按 instance id 查找、稳定节点地址以及可预测的 O(log n) 查找。

## ref 与 pin 是两类不同保证

~~~text
ref
= 长期 ownership / child relationship

pin
= 某次 API 调用期间临时保证对象不消失
~~~

典型 API 形状：

~~~c
if ((rc = dds_entity_pin(
       entity,
       &e)) != DDS_RETCODE_OK)
  return rc;

/* safe access to e */

dds_entity_unpin(e);
~~~

已经 pin 的调用允许完成；删除开始以后，新调用不能无条件拿到对象。这样就不需要所有 API 都长期持一把全局互斥锁。

## dds_delete 不是 free

公开入口只是：

~~~c
dds_return_t dds_delete(
  dds_entity_t entity)
{
  return dds_delete_impl(
    entity,
    DIS_USER);
}
~~~

内部先执行 delete-specific pin，之后才进入 close/delete 协议。过程中要处理 children、non-child refs、callbacks、observers、DDSI endpoint、implicit parent 与同步原语。

真正内存回收发生在所有前置条件成立以后：

~~~c
dds_entity_final_deinit_before_free(e);
dds_free(e);
~~~

## deriver vtable 是 C 的运行时多态

Writer、Reader、WaitSet、Domain 的关闭动作完全不同，所以公共 Entity 保存派生操作：interrupt、close、delete、set_qos、status/callback hooks。

WaitSet 要解除 observer，Writer 要关闭 DDSI writer，Domain 要停协议线程。用函数指针表将公共生命周期和具体实体逻辑分开，本质上就是 C ABI 版本的 virtual interface。

## Participant 创建同时跨 DDSc 与 DDSI

dds_participant.c 先进入 DDSI 线程状态并创建协议 Participant：

~~~c
ddsi_thread_state_awake(...);

ddsi_generate_participant_guid(...);

ret = ddsi_new_participant(
  &ddsi_guid,
  &dom->gv,
  flags,
  &plist);

ddsi_thread_state_asleep(...);
~~~

随后才分配 DDSc participant 并注册到 Domain child tree。

因此一个 Participant 同时具有 application lifetime 与 protocol/discovery lifetime。关闭时必须把两边一起收束。

## 对 ROS 2 shutdown 的意义

~~~text
停止接受新工作
-> 唤醒阻塞线程
-> 等待在途访问
-> 移除协议 endpoint
-> 回收 children
-> 销毁 mutex/cond
-> free
~~~

理解 Entity 生命周期后，再看 ROS 2 context shutdown 与 waitset wakeup，会更容易区分“上层 executor 协议”和“底层对象存活保证”。
