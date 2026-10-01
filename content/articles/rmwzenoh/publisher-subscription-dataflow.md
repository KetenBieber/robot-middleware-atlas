# Publisher 与 Subscription 数据面：从 CDR、Attachment 到本地 History

本文固定源码为 `rmw_zenoh@3b5b9bf424443f9800dd148b5f1cc2053bbc37fe`。这一篇沿同一条 ROS message 跟踪 publish 与 receive，重点看 key、payload ownership、attachment、history 与 queue。

## 1. PublisherData 创建时先完成语义固化

`PublisherData::make` 需要确定 topic name、ROS message type、type hash、QoS、domain、node/entity identity、Zenoh key expression 与 liveliness token。

所以 PublisherData 不是轻量 wrapper，而是 ROS endpoint contract 的具体化对象。

## 2. 为什么业务 payload 仍然使用 CDR

Zenoh 本身只传 bytes。

rmw_zenoh 选择 CDR，因为 ROS 2 已经有 CDR typesupport，也便于与 DDS bridge 互通。

~~~text
ROS object
   |
rosidl typesupport
   |
CDR bytes
   |
Zenoh payload
~~~

换 transport 并没有自动消除 serialization。

## 3. Attachment 为什么独立于 payload

RMW 还要返回 source timestamp、publisher GID、sequence number 与 received timestamp。

这些不是用户 message schema 字段。

因此 Publisher 把协议元数据编码到 Zenoh attachment：

~~~text
payload = CDR(message)

attachment =
  sequence_number
  source_timestamp
  publisher GID
~~~

Subscription 再解析 attachment 填充 `rmw_message_info_t`。

## 4. sequence number 为什么属于 PublisherData 状态

sequence 是 publisher 维度的单调状态，不应由每次临时 publish stack frame 自己决定。

如果多个线程可能并发 publish，就必须通过原子或锁维护唯一递增语义。

## 5. receive callback 的第一责任是取得所有权

Subscription endpoint callback 收到借用的 Zenoh Sample 后，不能把 Sample 引用直接存进未来才消费的 ROS queue。

必须转成拥有型状态：

~~~text
borrowed Zenoh Sample
   |
extract payload / attachment / timestamp
   |
owned SubscriptionData::Message
   |
message_queue_
~~~

否则 callback 返回后借用内存可能失效。

## 6. 为什么 callback 捕获 weak_ptr

Subscription endpoint callback 捕获 `weak_ptr<SubscriptionData>`。

~~~text
weak_ptr.lock()
   |
success -> temporary strong ref
failure -> entity destroyed, return
~~~

这避免异步 callback 捕获裸 `this` 后访问已经销毁的对象。

## 7. History policy 在哪里落地

`SubscriptionData::add_new_message` 在 subscription mutex 下操作 `message_queue_`。

当 QoS 不是 KEEP_ALL 且 queue 已达到 depth：

~~~text
pop_front(oldest)
push_back(newest)
~~~

所以这个固定版本直接在 RMW 本地队列中实现一部分 ROS history 语义。

它不是 DDS Reader History，但对 Executor 表现为类似的“可取样本历史”。

## 8. 为什么状态流更关心 oldest age

只看 queue size 不能判断系统是否新鲜。

两条队列都 depth=5，但最老消息可能分别是 3 ms 和 400 ms。

对控制链，更有意义的是：

~~~text
queue depth
+
oldest source timestamp age
~~~

## 9. add_new_message 为什么不能直接运行用户 callback

固定实现的职责链：

~~~text
Zenoh receive callback
   |
add_new_message()
   |
queue ownership
   |
data notification
   |
WaitSet wakeup
~~~

真正 ROS callback 仍由 Executor 线程通过 `rmw_take` 后执行。

这样 transport thread 不被用户 callback WCET 污染。

## 10. typed publish 与 serialized publish 为什么必须收敛

RMW 同时支持：

~~~text
rmw_publish(typed message)
rmw_publish_serialized_message(bytes)
~~~

两条入口最终都必须生成兼容的 Zenoh payload 与 attachment，否则 rosbag、bridge 与 serialized subscription 会得到不同 wire semantics。

## 11. 数据面的核心不变量

1. key identity 与 ROS endpoint contract 一致；
2. payload 生命周期覆盖异步发送；
3. attachment 与 payload 属于同一次 sample；
4. receive callback 返回后 queue 仍拥有数据；
5. history 只修改 queue ownership，不直接运行用户代码。

这些不变量比“调用 Publisher::put”更接近真正的数据面设计。
