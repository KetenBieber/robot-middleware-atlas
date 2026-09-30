# 项目案例：eCAL–MQTT Bridge 如何连接车内总线与云端消息系统

`eclipse-ecal/ecal-mqtt-bridge` 是一个真实桥接应用：一侧使用 eCAL 的高速本地发布订阅，另一侧使用 MQTT 接入更广域的消息基础设施。它展示了中间件桥接最重要的问题不是调用两个 publish API，而是命名、线程、序列化、回压和关闭语义的转换。

本文固定源码为 `eclipse-ecal/ecal-mqtt-bridge@a377003d2a83f8c693b1284378b79070e001254a`，沿配置校验、对象构造、双向转发、后台线程一直走到析构，并区分“代码现在做了什么”和“可靠桥接器还必须补什么”。

## 功能边界

eCAL 适合主机内/局域网高吞吐数据，MQTT 适合 broker-centric 的跨网络分发。Bridge 从 YAML 读取映射，将选定 eCAL topics 与 MQTT topics 对应，并管理两套运行时。

```text
eCAL Subscriber callback
  -> mapping / optional serialization
  -> bounded handoff
  -> MQTT publish

MQTT callback
  -> mapping / validation
  -> eCAL Publisher::Send
```

项目 README 明确给出三类需要桥接的消息：业务 payload、payload 的类型信息、descriptor string。这个细节揭示了桥接器的真实职责：它不只转发字节，还要让 MQTT 一侧有机会重建 eCAL 消息的类型语境。配置也允许用静态 eCAL type name 代替从 MQTT 侧接收类型名。

## 仓库入口与调用链

仓库的主调用链保持线性：

```text
可执行入口
  -> 命令行参数：解析 -c 与 -v
  -> YAML 配置对象：broker 与 topic mapping
  -> eCAL 初始化和实体创建
  -> Mosquitto client 初始化与 network loop
  -> 两个方向的 callback
  -> 退出信号与逆序清理
```

先从配置反推对象数量：每条 eCAL→MQTT 映射至少需要 eCAL subscriber 与 MQTT 目标；每条 MQTT→eCAL 映射至少需要 MQTT subscription 与 eCAL publisher。双向桥不是“一个 subscriber 加一个 publisher”，而是配置驱动的实体集合。

先建立运行对象间的关系：

| 运行阶段 | 关键操作 | 保存或移动的数据 |
|---|---|---|
| 配置进入运行时 | `CheckYamlValidity()`、`run()` | broker 与 topic 映射值、Bridge owner 集合 |
| 创建一侧实体 | `Bridge::initEcal()`、`Bridge::initMqtt()` | eCAL Publisher/Subscriber 与 MQTT client |
| MQTT 到 eCAL | `Bridge::on_message()` | MQTT payload、类型名、descriptor 与目标 Publisher |
| eCAL 到 MQTT | `Bridge::ecalMessageReceived()` | eCAL 借用 payload、topic 映射与 MQTT publish 调用 |
| 元数据与关闭 | `Bridge::descriptorUpdateLoop()`、`Bridge::~Bridge()` | descriptor 状态、worker、两套库的停止顺序 |

下面回到 `CheckYamlValidity()`。这段固定提交源码摘录覆盖整个校验函数，能同时看见三处容易被一行教学代码掩盖的状态问题。


```cpp
bool CheckYamlValidity(std::map<std::string,Broker>& brokers,
                       std::map<std::string,MqttTopic>& mqtt2ecal_topics,
                       std::map<std::string,EcalTopic>& ecal2mqtt_topics)
{
    // Check if we have any valid brokers
    if (brokers.size() == 0)
    {
        printError("No brokers configured");
        return false;
    }

    // Check if the broker name is valid for mqtt --> ecal topics
    // If not, delete from mqtt2ecal topics list
    for (auto it = mqtt2ecal_topics.cbegin(); it != mqtt2ecal_topics.cend();)
    {
        if (brokers.count(it->second.broker_name) != 1)
        {
            printError("No broker with name " + it->second.broker_name + " configured for topic " + it->first);
            mqtt2ecal_topics.erase(it++);
        }
        else
        {
            ++it;
        }
    }

    // Check if the broker name is valid for ecal --> mqtt topics
    // If not, delete from ecal2mqqt topics list
    for (auto it = ecal2mqtt_topics.cbegin(); it != ecal2mqtt_topics.cend();)
    {
        if (brokers.count(it->second.broker_name) != 1)
        {
            printError("No broker with name " + it->second.broker_name + " configured for topic " + it->first);
            ecal2mqtt_topics.erase(it++);
        }
        else
        {
            ++it;
        }
    }

    // Throw out any brokers that are not used
    bool found = false;
    for (auto it = brokers.cbegin(); it != brokers.cend();)
    {
        for (auto topic : mqtt2ecal_topics)
        {
            if (topic.second.broker_name == it->second.name)
            {
                found = true;
            }
        }
        for (auto topic : ecal2mqtt_topics)
        {
            if (topic.second.broker_name == it->second.name)
            {
                found = true;
            }
        }
        if (!found)
        {
            printError("Broker " + it->second.name + " is not used");
            brokers.erase(it++);
        }
        else
        {
            ++it;
        }
    }

    // if no quality of service is defined to eCAL -> MQTT, use the default one from the corresponding broker
    // if no retain_flag is set, use the default one from the corresponding broker
    for (auto topic : ecal2mqtt_topics)
    {
        if (!topic.second.is_set_retain_flag || topic.second.qos == -1)
        {
            for (auto broker : brokers)
            {
                if (broker.second.name == topic.second.broker_name)
                {
                    if (!topic.second.is_set_retain_flag)
                    {
                        topic.second.retain_flag = broker.second.default_retain_flag;
                    }
                    if (topic.second.qos == -1)
                    {
                        topic.second.qos = broker.second.default_qos;
                    }
                }
            }
        }
    }

    // if no quality of service is defined to MQTT -> eCAL, use the default one from the corresponding broker
    for (auto topic : mqtt2ecal_topics)
    {
        if (topic.second.qos == -1)
        {
            for (auto broker : brokers)
            {
                if (broker.second.name == topic.second.broker_name)
                {
                    topic.second.qos = broker.second.default_qos;
                }
            }
        }
    }
    return true;
}
```

摘录中的 range-for 用 `auto topic` 复制 map 的 `pair<const std::string, Topic>`。`topic.second.qos` 或 `retain_flag` 改到的是这份副本，函数返回后原 map 仍保留 sentinel `-1` 和原默认值；而每一次复制 Topic 值还会复制它所拥有的字符串。若改为 `auto& [name, topic]`，才是在原容器元素上修改。另一个独立错误是 `found` 在 broker 循环外只初始化一次：一旦某个 broker 匹配到 route，后续未使用 broker 会继承 `true`，不会被删除。多 broker 配置下，这会让启动器保留空 Bridge，而不是按注释清除无映射 broker。

下面这段来自 `EcalTopic::CheckValidity`；`MqttTopic::CheckValidity` 与 `Broker::CheckValidity` 也使用同样的 QoS 条件。

接着看 `EcalTopic::CheckValidity` 的真实实现：

```cpp
bool EcalTopic::CheckValidity()
{
    if (broker_name.empty() || ecal_topic_name.empty() || mqtt_out_payload_name.empty())
        return false;

    // check if qos is in range [0,2]
    if (qos < 0 && qos > 2)
        return false;
    return true;
}
```

一个整数不可能同时小于 `0` 且大于 `2`，所以非法 QoS 不会被这个条件拒绝。可靠的范围谓词应是 `qos < 0 || qos > 2`；若 `-1` 是“等待继承默认值”的 sentinel，则更好的设计是先保留 `std::optional<int>`，完成默认合并后再验证最终值，避免把合法的中间态与错误输入混在一个整数域里。

运行时对象图是“一 broker 一 Bridge”：

```text
run()
  -> parse YAML into maps
  -> filter mappings by broker
  -> Bridge(broker A, mappings A)
       |-- mosquittopp base/client loop
       |-- eCAL subscribers for eCAL->MQTT
       |-- eCAL publishers for MQTT->eCAL
       `-- descriptorUpdateLoop thread
  -> Bridge(broker B, mappings B)
       `-- same object family
```

这张图同时暴露一个设计问题：eCAL 和 `mosqpp::lib_init/lib_cleanup` 都是进程级生命周期，当前却由每个 Bridge 构造和析构。单 broker 主路径可以工作；多 broker 时应把全局运行时提升到进程 owner，Bridge 只拥有 broker session 与映射实体。

## 启动阶段的真实调用链

`main()` 解析 `-c=PATH` 与 `-v`，默认从可执行文件旁读取 `settings.yaml`。`run()` 将 YAML 分成 general settings、brokers、`mqtt2ecal` 和 `ecal2mqtt`，再将每个节点转换成值对象并放入 `std::map`。之后 `CheckYamlValidity()` 删除引用不存在 broker 的映射和未使用 broker，最后为每个 broker 筛选自己的 mapping vector 并创建一个 `unique_ptr<Bridge>`。

```text
main
  -> YAML::LoadFile
  -> node >> Broker / MqttTopic / EcalTopic
  -> CheckValidity on each value
  -> CheckYamlValidity across references/defaults
  -> for each broker
       -> Bridge constructor
            -> descriptor thread starts
            -> initEcal
            -> initMqtt
                 -> mosqpp::lib_init
                 -> TLS/auth/options
                 -> connect/connect_async
                 -> loop_start
```

`Bridge::initEcal()` 先全局 `eCAL::Initialize`，然后为每条 eCAL→MQTT mapping `new CSubscriber` 并注册同一个 callback；为每条 MQTT→eCAL mapping `new CPublisher`。如果配置要求转发 type/descriptor，它还注册 eCAL publisher registration callback，从监控 Sample 中取得 `ttype/tdesc`。

下面把“mapping 变成运行时对象”的实际函数直接放在正文里。输入是构造函数保存在 Bridge 中的两组配置 vector；输出不是一个抽象 route，而是若干 Subscriber/Publisher、两类 callback 注册和全局 eCAL runtime。

接着看 `Bridge::initEcal` 的真实实现：

```cpp
bool Bridge::initEcal(int argc, char** argv)
{
    printVerbose("************************************************************************");
    printVerbose(add_spacing("eCAL settings"));
    printVerbose("Process name: " + general_settings.ecal_process_name);

    if (eCAL::Initialize(argc, argv, general_settings.ecal_process_name.c_str()) == -1)
    {
        printError("Failed to initialize eCAL");
        return false;
    }
    // Create eCAL Subscribers
    auto callback = std::bind(&Bridge::ecalMessageReceived, this, std::placeholders::_1, std::placeholders::_2);
    for (auto topic : ecal2mqtt_topics)
    {
        eCAL::CSubscriber* sub = new eCAL::CSubscriber(topic.ecal_topic_name);
        sub->AddReceiveCallback(callback);
        ecal_subscribers.push_back(sub);
        printVerbose("Creating eCAL subscriber : " + topic.ecal_topic_name);
    }
    // Create eCAL Publishers
    for (auto topic : mqtt2ecal_topics)
    {
        std::string topic_type = "";

        if (!topic.static_ecal_type_name.empty())
        {
            topic_type = topic.static_ecal_type_name;
        }

        printVerbose("Creating eCAL publisher : " + topic.ecal_out_topic_name + " (" + topic_type + ")");
        ecal_publishers[topic.ecal_out_topic_name] = (new eCAL::CPublisher(topic.ecal_out_topic_name, topic_type, ""));
    }

    for (auto topic : ecal2mqtt_topics)
    {
        if (!topic.mqtt_out_descriptor.empty())
        {
            // If we need to send a descriptor info via MQTT we need a monitoring info to get the descriptor string, so we work with a event + registration callback
            eCAL::Process::AddRegistrationCallback(reg_event_publisher, std::bind(&Bridge::onPublisherRegistration, this, std::placeholders::_1, std::placeholders::_2));
            break;
        }
    }
    return true;
}
```

`std::bind` 生成一个可调用对象，`std::placeholders` 把接收时才出现的 topic 与 sample 指针留作参数；`AddReceiveCallback` 将它存到 eCAL 的 callback 接口里。这里绑定的是裸 `this`，没有捕获 `shared_ptr<Bridge>`，因此 middleware 不会因此延长 Bridge 生命周期。Subscriber 和 Publisher 也用裸 `new`，原始指针随后分别放进 vector/map，所有权只能靠手工 `delete` 约定表达；若实体构造或 callback 注册在部分成功后抛出异常，未完成构造的 Bridge 不会执行自己的析构函数体，原始指针容器不会自动回滚。`unique_ptr` 容器和明确的两阶段 `Start()` 能让部分启动失败按 RAII 逆序收回资源。

这个对象关系还连接到稍后的析构时间线：一个正在运行的 callback 持有的是 `Bridge*` 的数值，不是 Bridge 的所有权租约。关闭必须先让 callback 入口停止并等待已经进入的调用返回，然后才能清理它访问的 Mosquitto client、publisher map 和 Bridge 本身。

`Bridge::initMqtt()` 初始化 Mosquitto 全局库，配置协议版本、用户名、证书或 PSK，然后连接 broker 并启动 Mosquitto network loop。只有 `on_connect(0)` 回调到达后，代码才订阅 payload/type/descriptor MQTT topics，并将连接状态置为 true。

固定版本的 `Bridge::on_connect` 把连接成功分支完整写在 callback 里；真正的输入是 broker 返回码，副作用是逐项发起三类 MQTT subscription，最后才把 `is_connected_to_mqtt_broker` 发布为 true。


```cpp
void Bridge::on_connect(int rc)
{
    printVerbose("on_connect rc: " + std::to_string(rc));
    switch (rc)
    {
    case 0:
    {
        printVerbose("Successfully connected to MQTT Broker");
        // Connect the "normal" mqtt subscriber
        for (auto topic : mqtt2ecal_topics)
        {
            int subscribe_err = subscribe(NULL, topic.mqtt_payload_name.c_str(), topic.qos);
            if (subscribe_err == MOSQ_ERR_SUCCESS)
            {
                printVerbose("Successfully subscribed MQTT topic: " + topic.mqtt_payload_name);
            }
            else
            {
                printError("Failed to subscribe to mqtt topic \"" + topic.mqtt_payload_name + "\"", subscribe_err, MOSQ_STR_ERROR);
                return;
            }
        }
        // Connect the "descriptor" mqtt subscriber
        for (auto topic : mqtt2ecal_topics)
        {
            if (topic.mqtt_ecal_type_descriptor.size() > 0)
            {
                int subscribe_err = subscribe(NULL, topic.mqtt_ecal_type_descriptor.c_str(), topic.qos);
                if (subscribe_err == MOSQ_ERR_SUCCESS)
                {
                    printVerbose("Successfully subscribed MQTT topic: " + topic.mqtt_ecal_type_descriptor);
                }
                else
                {
                    printError("Failed to subscribe to mqtt topic \"" + topic.mqtt_ecal_type_descriptor + "\"", subscribe_err, MOSQ_STR_ERROR);
                    return;
                }
            }
        }
        // Connect the "type" mqtt subscriber
        for (auto topic : mqtt2ecal_topics)
        {
            if (topic.mqtt_ecal_type_name.size() > 0)
            {
                int subscribe_err = subscribe(NULL, topic.mqtt_ecal_type_name.c_str(), topic.qos);
                if (subscribe_err == MOSQ_ERR_SUCCESS)
                {
                    printVerbose("Successfully subscribed MQTT topic: " + topic.mqtt_ecal_type_name);
                }
                else
                {
                    printError("Failed to subscribe to mqtt topic \"" + topic.mqtt_ecal_type_name + "\"", subscribe_err, MOSQ_STR_ERROR);
                    return;
                }
            }
        }
        is_connected_to_mqtt_broker = true;
        break;
    }
    case 1:
    {
        printError("Connection refused while connecting to MQTT Broker (unacceptable protocol version)");
        break;
    }
    case 2:
    {
        printError("Connection refused while connecting to MQTT Broker (identifier rejected)");
        break;
    }
    case 3:
    {
        printError("Connection refused while connecting to MQTT Broker (broker unavailable)");
        break;
    }
    case 4:
    {
        printError("Connection refused while connecting to MQTT Broker (bad user name or password)");
        break;
    }
    case 5:
    {
        printError("Connection refused while connecting to MQTT Broker (not authorised)");
        break;
    }
    default:
    {
        printError("Connection Error unknown: " + std::to_string(rc));
        break;
    }
    }
}
```

这段在 callback 中按 vector 的顺序逐条发订阅请求；它没有把全部 route 作为一个 broker 级事务，因此第 `k` 项失败时，前面已经成功的 subscription 不会被回滚，但 `is_connected_to_mqtt_broker` 仍没有置 true。下一次 `on_connect(0)` 会从第一项重新发起。循环变量仍是 `auto topic`，每轮复制映射值；启动只发生在连接事件，频率远低于 payload callback，但配置很大时仍会产生可见复制。

### 配置解析中的 C++ 值语义陷阱

固定提交提供了很好的反例。`CheckYamlValidity()` 想把 broker 默认 QoS/retain 写回每个映射，但循环写成 `for (auto topic : ecal2mqtt_topics)`；`topic` 是 pair 的副本，对 `topic.second` 的修改不会回到 map。应使用 `auto& [name, topic]`：

**教学/复刻示例（不是固定提交源码摘录）：**

```cpp
for (auto& [name, topic] : ecal2mqtt_topics) {
  const Broker& broker = brokers.at(topic.broker_name);
  if (!topic.is_set_retain_flag) topic.retain_flag = broker.default_retain_flag;
  if (topic.qos == -1) topic.qos = broker.default_qos;
}
```

这是 C++ range-for 中非常实际的差别：`auto` 复制元素，`auto&` 才修改容器。若值对象很大，无意复制还会增加启动成本。

三个 `CheckValidity()` 的 QoS 判断均写成 `qos < 0 && qos > 2`，这个条件不可能同时为真；正确范围拒绝应为 `qos < 0 || qos > 2`，同时要区分 `-1` 是否代表“尚未继承默认值”。把 sentinel、校验和默认合并顺序设计清楚，比在末尾补一个 if 更重要。

删除未使用 broker 的循环还把 `found` 放在循环外，找到一个已使用 broker 后没有为下一个 broker复位，可能保留后续未使用项。更稳妥的写法是每个 broker 用 `std::any_of` 重新计算，或者在解析后建立 `broker_name -> mappings` 索引，从结构上消除嵌套搜索。

### 理想启动应形成一笔配置事务

一个可维护的启动骨架如下，代码用于还原项目结构而不是逐字复制仓库：

**教学/复刻示例（不是固定提交源码摘录）：**

```cpp
int Run(const Options& options) {
  const Config cfg = ParseAndValidateYaml(options.config_path);
  EcalRuntime ecal{"mqtt_ecal_bridge"};
  MqttRuntime mqtt{cfg.broker};

  Bridge bridge{ecal, mqtt};
  for (const auto& mapping : cfg.mappings) {
    bridge.AddMapping(mapping);
  }
  bridge.Start();
  WaitForShutdownSignal();
  bridge.Stop();
  return 0;
}
```

关键不是 RAII 外观，而是 `ParseAndValidateYaml()` 必须先验证所有必填字段、重复目标、非法方向和类型策略，再创建任何网络实体。否则第 20 条映射失败时，前 19 条已经对外可见，系统进入难以解释的半配置状态。

`EcalRuntime` 与 `MqttRuntime` 的声明顺序也决定析构逆序：Bridge 必须先销毁，随后 MQTT/eCAL 全局运行时才可结束。若先 Finalize eCAL，Bridge 析构中注销 subscriber 就可能访问已经关闭的全局状态。

## 当前同步快路径与异步边界

固定提交没有为 payload 建立应用级有界队列。eCAL callback `ecalMessageReceived()` 遍历全部 eCAL→MQTT mappings，匹配 topic 后直接调用 Mosquitto `publish()`；Mosquitto `on_message()` 同样遍历 MQTT→eCAL mappings，匹配后直接调用对应 `CPublisher::Send()`。

```text
eCAL delivery thread
  -> Bridge::ecalMessageReceived
  -> O(E) mapping scan
  -> mosquittopp::publish

Mosquitto loop thread
  -> Bridge::on_message
  -> O(M) mapping scan
  -> CPublisher::SetTypeName / SetDescription / Send
```

其中 `E/M` 是两个方向的映射数。这个实现短小，且 Mosquitto 客户端自身有网络 loop；但 callback 仍承担查找、构造 `std::string`、可能的哈希/映射修改和 library API 调用。文章不能把“建议加入有界队列”误写成仓库已经有异步 handoff。

先沿 MQTT→eCAL 方向看完整 callback。它收到的 `message` 是 Mosquitto 回调参数；函数没有把整个消息放进 Bridge 自己的队列，而是当场扫描 route、更新类型元数据或调用 eCAL `Send()`。

接着看 `Bridge::on_message` 的真实实现：

```cpp
void Bridge::on_message(const struct mosquitto_message* message)
{
    if ((is_initialized == false) || (is_connected_to_mqtt_broker == false))
    {
        return;
    }
    // Check if the message that has arrived is a descriptor message or type name
    // if so check if the descriptor hash or type name hash is in the table
    MqttTopic current_topic;

    bool found_descriptor = false;
    bool found_type       = false;
    bool found_payload    = false;

    for (auto topic : mqtt2ecal_topics)
    {
        if (topic.mqtt_ecal_type_descriptor == std::string(message->topic))
        {
            current_topic    = topic;
            found_descriptor = true;
        }
        else if (topic.mqtt_ecal_type_name == std::string(message->topic))
        {
            current_topic = topic;
            found_type    = true;
        }
        else if(topic.mqtt_payload_name == std::string(message->topic))
        {
            current_topic  = topic;
            found_payload  = true;
        }
    }

    if (found_descriptor)
    {
        std::string descriptor(static_cast<char*>(message->payload), message->payloadlen);
        auto hash = hasher(descriptor);
        auto current_topic_hash = from_mqtt_desc_hash.find(std::string(message->topic));

        if (current_topic_hash == from_mqtt_desc_hash.end() || current_topic_hash->second != hash)
        {
            from_mqtt_desc_hash[message->topic] = hash;
            auto pub_it = ecal_publishers.find(current_topic.ecal_out_topic_name);
            if (pub_it != ecal_publishers.end())
            {
                pub_it->second->SetDescription(descriptor);
            }
        }
    }
    else if (found_type)
    {
        std::string topic_type(static_cast<char*>(message->payload), message->payloadlen);
        auto hash = hasher(topic_type);
        auto current_topic_hash = from_mqtt_type_hash.find(std::string(message->topic));

        if (current_topic_hash == from_mqtt_type_hash.end() || current_topic_hash->second != hash)
        {
            from_mqtt_type_hash[message->topic] = hash;
            auto pub_it = ecal_publishers.find(current_topic.ecal_out_topic_name);
            if (pub_it != ecal_publishers.end())
            {
                pub_it->second->SetTypeName(topic_type);
            }
        }
    }
    else if (found_payload)
    {
        auto pub_it = ecal_publishers.find(current_topic.ecal_out_topic_name);
        if (pub_it != ecal_publishers.end())
        {
            mqtt_rx_counter++;
            pub_it->second->Send(message->payload, message->payloadlen);
        }
    }
}
```

当 topic 是 descriptor 或 type 时，构造 `std::string(pointer, length)` 会复制回调传入的 bytes；哈希表只用来跳过内容未变化的 setter。当 topic 是 payload 时，代码直接把 MQTT 回调中的裸 payload 指针传给 `CPublisher::Send()`，而不是把指针保存到异步工作项。eCAL 的 Send 路径必须在调用返回前消费或复制这份输入；上游 eCAL `CPublisher::Send` 与 `CPublisherImpl::Write` 的复制条件在《Publisher 发送链》中直接展示。Bridge 本身没有为 payload 延长 MQTT 回调缓冲区寿命。

这个 callback 还明确暴露出负载增长成本：`for (auto topic : mqtt2ecal_topics)` 按值复制每个 `MqttTopic`，包含它的字符串；每次比较又构造 `std::string(message->topic)`。映射有 `M` 条时，除了 `O(M)` 次比较，还包含与字符串大小相关的复制/分配。循环没有在命中后 `break`，重复配置还可能让 `current_topic` 指向最后一个匹配项；如果 descriptor、type、payload topic 名冲突，之后的 `if/else if` 又按 descriptor、type、payload 的优先级决定实际操作。启动期构造三个不可变哈希索引可减少回调工作，但这是改进方案，不是当前代码。

反方向的 callback 更短，也把另一个失败边界暴露得很清楚。

接着看 `Bridge::ecalMessageReceived` 的真实实现：

```cpp
void Bridge::ecalMessageReceived(const char* topic_name_, const struct eCAL::SReceiveCallbackData* data_)
{
    if (!is_initialized || !is_connected_to_mqtt_broker) return;
    for (auto topic : ecal2mqtt_topics)
    {
        if (topic.ecal_topic_name == std::string(topic_name_)) {
            publish(NULL, topic.mqtt_out_payload_name.c_str(), data_->size, data_->buf, topic.qos, topic.retain_flag);
        }
    }
    ecal_rx_counter++;
}
```

`topic_name_` 与 `data_->buf` 都是 callback 参数指针；本函数没有复制 payload，也没有保留这些指针。它在 callback 返回前把借用视图交给 Mosquitto `publish()`，随后无论有没有 route 命中，都会递增普通整数 `ecal_rx_counter`；`publish()` 的返回码也被忽略。因此该计数不是成功转发数，不能用它证明 broker 接受了消息。逐条 mapping 同样按值复制 route。对于慢 broker，实际停顿会出现在调用 `publish()` 的 eCAL 交付线程上，固定代码里没有隔离它的 Bridge worker。

映射应在启动时预编译成三个索引：eCAL payload topic、MQTT payload topic、MQTT type/descriptor topic分别到不可变 Route。这样 callback 平均查找为 `O(1)`，并能在启动时拒绝同一个 MQTT topic 同时被声明为 payload 与 descriptor。当前 `on_message()` 使用 `if descriptor / else if type / else if payload`，冲突名称会由分支优先级暗中决定。

### payload 跨 callback 的所有权

MQTT publish 可能受 broker、网络和 QoS 影响。若在 eCAL Subscriber callback 中同步等待，慢云连接会阻塞 eCAL 接收交付。反向路径同理，eCAL 发送和实体重建不应占用 MQTT network loop。

桥接器应为两个方向各设有界队列，并为状态流和事件流选择不同溢出策略。只使用一个无限队列会把网络故障转成内存故障。

可把 callback 收缩为“复制必要元数据并尝试入队”：

**教学/复刻示例（不是固定提交源码摘录）：**

```cpp
void OnEcalMessage(std::string_view topic,
                   ByteView payload,
                   std::int64_t timestamp) {
  Envelope item{std::string(topic),
                std::vector<std::byte>(payload.begin(), payload.end()),
                timestamp};
  if (!to_mqtt_.try_push(std::move(item))) {
    metrics_.ecal_to_mqtt_dropped.fetch_add(1);
  }
}
```

这里发生一次显式复制，因为 eCAL callback 返回后 payload 视图未必继续有效。若追求零复制，需要由两端共同支持可转移的共享缓冲区；仅把裸指针放进队列不是优化，而是悬空引用。

队列元素是值对象，worker 取得后不依赖 callback 栈。`try_push` 避免 eCAL 交付线程被 MQTT 阻塞，但它选择了“满载时失败”。状态 topic 可以丢旧保新，审计事件则可能要求落盘或反压；配置必须允许按映射选择。

## 配置驱动的实体生命周期

一条映射至少定义源 topic、目标 topic、方向、payload 类型和 QoS。加载配置时先验证全部条目，再批量创建 Publisher/Subscriber；中途失败需要逆序回滚。

当前 Bridge 将 broker settings 和两个 mapping vectors 以 `const` 值成员保存，所以运行期没有热重载；callback 遍历的配置对象在 Bridge 生命周期内地址稳定。这个选择减少了并发变更，却意味着修改一条 route 必须重启整个 broker Bridge。

实体数可以从配置直接估算。某 broker 有 `E` 条 eCAL→MQTT 和 `M` 条 MQTT→eCAL 映射时，当前代码创建 `E` 个 `CSubscriber`、`M` 个 `CPublisher`、一个 Mosquitto client/network loop，以及每个 Bridge 一个 descriptor worker。type/descriptor 使用同一个 MQTT client 的附加订阅，不额外创建 eCAL publisher。

当前所有权形式是：

**教学/复刻示例（不是固定提交源码摘录）：**

```cpp
std::vector<eCAL::CSubscriber*> subscribers;          // 手工 delete
std::map<std::string, eCAL::CPublisher*> publishers;  // 手工 delete
std::list<std::unique_ptr<Bridge>> bridges;            // Bridge 本身 RAII
```

外层已经使用 `unique_ptr<Bridge>`，内层仍用裸 owning pointer。若第二十个实体构造或后续 MQTT 初始化失败，Bridge 仍需依靠析构遍历清理。改成 `vector<unique_ptr<CSubscriber>>` 与 `unordered_map<string, unique_ptr<CPublisher>>` 后，部分构造失败会自动逆序释放，也能从类型上区分 owner 与观察指针。

### type 与 descriptor 的独立控制流

eCAL→MQTT 方向不能从普通 payload callback 直接取得 schema。代码注册 `eCAL::Process::AddRegistrationCallback(reg_event_publisher, ...)`，解析 eCAL monitoring protobuf `eCAL::pb::Sample`，当 publisher topic 匹配 mapping 时，把 `ttype` 与 `tdesc` 保存到两个 map：

```text
eCAL publisher registration event
  -> parse eCAL::pb::Sample
  -> find interested route
  -> mqtt_type_topics[mqtt_type_topic] = ttype
  -> mqtt_descriptor_topics[mqtt_descriptor_topic] = tdesc

descriptorUpdateLoop
  -> every ~5 seconds when connected
  -> publish retained/non-retained metadata according to route
```

两张 map 分别有 mutex，因为 registration callback 与 descriptor worker 并发。可是 worker 在持 mutex 时执行 `publish()`，并且每项后 `sleep_for(10ms)`；metadata 数量多或 MQTT 调用变慢时，registration callback 会长时间等锁。更好的模式是锁内复制/交换待发快照，解锁后 publish。

MQTT→eCAL 方向则在 `on_message()` 中先处理 descriptor/type：计算 `std::hash<string>`，只在内容 hash 变化时调用 `CPublisher::SetDescription` 或 `SetTypeName`。payload 到达后直接 `Send`。这带来一个时序问题：payload 可能早于 retained metadata 到达，eCAL consumer 会先看到类型尚未设置的 publisher。可以让 route 在 type/descriptor 条件满足前缓存有限数量 payload，或把 schema id 放入同一个 envelope，不能假设不同 MQTT topics 之间有全局顺序。

`std::hash<string>` 只用来避免重复 setter，不是稳定协议哈希：标准不保证跨实现/进程保持相同结果，也不提供抗碰撞语义。这里 hash 只活在一个进程内，尚可作为缓存提示；若要把 schema identity 放上 wire，应使用明确算法和版本。

热重载不能直接清空 map：callback 可能仍持有旧 mapping。可用不可变 `shared_ptr<const MappingTable>` 快照，构建新表后原子替换，旧 callback 完成后自然释放。

更完整的热重载分为三组：保留项复用实体，新增项先创建但暂不启用，删除项先从新快照移除再等待旧 callback 退出。新表全部准备成功后才一次发布；任一新增项失败则销毁暂存实体，旧表继续服务。

MappingTable 的查找平均可做到 `O(1)`，但复制整个不可变表的成本为 `O(n)`。热重载属于低频控制面，这个成本换来了 callback 无需在全局互斥量下做网络工作。

## 数据语义转换

eCAL Core 传输二进制 blob，MQTT payload 也是 bytes，但两端元数据不同。Bridge 必须决定是否携带 eCAL type/schema、timestamp、publisher id 和 sequence。原样转发 payload 只能保证字节到达，不能保证远端知道如何解码。

MQTT retained message 与 eCAL 普通 publication 也不同。若启用 retained，晚加入消费者会收到旧值；桥接配置必须显式标记，不可默认推断。

还要分别回答：

| 语义 | eCAL 一侧 | MQTT 一侧 | 桥接策略 |
|---|---|---|---|
| 类型 | topic 元数据、descriptor | payload 与 topic 本身不规定 schema | 转发 type/descriptor 或配置静态类型 |
| 可靠性 | 取决于所选 transport | QoS 0/1/2 | 不宣称端到端强于最弱链路 |
| 重复 | 传输层语义 | QoS 重投可能重复 | envelope 带 message id，消费者幂等 |
| 保留 | 普通流不等于 retained | broker 可保留最后消息 | 每条映射显式开关 |
| 顺序 | 受 publisher 与 transport 影响 | 单连接/topic 通常有局部顺序 | 不虚构跨 topic 全局顺序 |
| 认证 | 局域网部署策略 | broker TLS/凭据 | 凭据不写进普通 YAML 或日志 |

固定提交实际只原样转发 payload bytes，并把 type 和 descriptor 放到独立 MQTT topics；eCAL callback 中的 timestamp、publisher id 等接收元数据没有进入 MQTT。因此它实现的是“payload 与部分类型语境的透明搬运”，不是完整 eCAL sample envelope。若远端需要端到端延迟、去重或源追踪，必须定义版本化 envelope，例如：

**教学/复刻示例（不是固定提交源码摘录）：**

```cpp
struct BridgeEnvelopeV1 {
  std::uint32_t magic;
  std::uint16_t version;
  std::uint16_t flags;
  std::uint64_t sequence;
  std::int64_t source_time_ns;
  std::array<std::byte, 16> publisher_id;
  std::uint32_t type_name_size;
  std::uint32_t descriptor_size;
  std::uint32_t payload_size;
  // 后接显式编码的变长字段，不直接 memcpy 本 struct
};
```

结构体只是字段清单，wire format 要逐字段定义端序、长度上限和兼容规则，不能序列化 C++ padding。若继续使用三个 MQTT topics，则必须定义 metadata 与 payload 的版本关联；单纯 retained type 加实时 payload，在 schema 更新瞬间可能组合出错误版本。

## 关闭、断线与恢复

固定提交析构顺序是：关闭 descriptor thread 标志、将 initialized 置 false、join worker、disconnect MQTT、停止 Mosquitto loop、`lib_cleanup`、delete eCAL publishers/subscribers、`eCAL::Finalize`。方向大体正确：先阻止桥接逻辑，再停外部回调，最后释放实体和全局运行时。

但 worker 用 50 次 `sleep_for(100ms)` 实现可中断的五秒等待，最坏析构仍需接近 100 ms，而不是立即被 condition variable 唤醒。更严重的是线程在成员初始化列表中由 `mqtt_desc_thread(&Bridge::descriptorUpdateLoop, this)` 启动，而控制它的 `mqtt_desc_thread_active` 在类声明中位于 thread 之后。C++ 按成员声明顺序初始化，线程可能在 atomic 完成构造前读取它，同时 `this` 也在构造完成前逃逸。

固定源码把字段声明顺序和启动位置写得很直接。下面展示同一条构造链：Bridge 声明 worker 与停止位，`Bridge::Bridge` 在成员初始化阶段启动线程，构造函数体随后才调用 `initialize()`。


```cpp
std::thread                               mqtt_desc_thread;
std::atomic<bool>                         mqtt_desc_thread_active;

Bridge::Bridge(int argc, char** argv,const Broker& broker, const std::vector<MqttTopic>& mqtt2ecal_topics, const std::vector<EcalTopic>& ecal2mqtt_topics, const GeneralSettings& general_settings, bool verbose)
    : mosquittopp(broker.id.c_str(), true /* clean session */)
    , general_settings(general_settings)
    , mqtt2ecal_topics(mqtt2ecal_topics)
    , ecal2mqtt_topics(ecal2mqtt_topics)
    , broker_settings(broker)
    , mqtt_desc_thread(&Bridge::descriptorUpdateLoop, this)
    , mqtt_desc_thread_active(true)
    , is_initialized(false)
    , is_connected_to_mqtt_broker(false)
    , loop_started(false)
    , mqtt_rx_counter(0)
    , ecal_rx_counter(0)
    , verbose(verbose)
{
    initialize(argc, argv);
}
```

C++ 成员按**类声明顺序**初始化，不按初始化列表中的书写位置重排。`mqtt_desc_thread` 在 `mqtt_desc_thread_active` 之前；构造 `std::thread` 会立刻让另一个执行流有机会运行，所以该执行流能在停止位的生命周期开始前读取它。线程还捕获 `this`，紧随其后的 `is_initialized` 与 `is_connected_to_mqtt_broker` 也尚未构造完成。atomic 只能约束一个已经存在对象上的原子访问，不能把“线程提前开始”修成合法对象生命周期。

线程进来后会先检查映射，再根据两个 atomic 决定是否发布 metadata；它在两把 mutex 的作用域里调用 MQTT `publish()`，每项之后 sleep 10 ms，外层最长每 100 ms 轮询一次停止位。完整函数揭示了锁域和实际等待方式。

接着看 `Bridge::descriptorUpdateLoop` 的真实实现：

```cpp
void Bridge::descriptorUpdateLoop()
{
    bool found = false;
    for (auto topic : ecal2mqtt_topics)
    {
        if (!topic.mqtt_out_descriptor.empty() || !topic.mqtt_out_type_name.empty())
        {
            found = true;
        }
    }
    if (found)
    {
        while (mqtt_desc_thread_active == true)
        {
            // Iterate through
            if (is_initialized && is_connected_to_mqtt_broker)
            {
                std::lock_guard<std::mutex> lock_desc(mqtt_desc_mtx);

                // iterate through descriptors
                for (auto const& mqtt_topic : mqtt_descriptor_topics)
                {
                    // iterate through topics, find the corresponding one and publish it to mqtt
                    for (auto topic : ecal2mqtt_topics)
                    {
                        if (topic.mqtt_out_descriptor == mqtt_topic.first)
                        {
                            publish(NULL, mqtt_topic.first.c_str(), static_cast<int>(mqtt_topic.second.size()), mqtt_topic.second.data(), topic.qos, topic.retain_flag);
                            std::this_thread::sleep_for(std::chrono::milliseconds(10));
                            break;
                        }
                    }
                }

                std::lock_guard<std::mutex> lock_type(mqtt_type_mtx);
                // iterate through types
                for (auto const& mqtt_topic : mqtt_type_topics)
                {
                    // iterate through topics, find the corresponding one and publish it to mqtt
                    for (auto topic : ecal2mqtt_topics)
                    {
                        if (topic.mqtt_out_type_name == mqtt_topic.first)
                        {
                            publish(NULL, mqtt_topic.first.c_str(), static_cast<int>(mqtt_topic.second.size()), mqtt_topic.second.data(), topic.qos, topic.retain_flag);
                            std::this_thread::sleep_for(std::chrono::milliseconds(10));
                            break;
                        }
                    }
                }
            }
            for (auto counter = 0; (mqtt_desc_thread_active == true) && (counter < 50); counter++)
            {
                std::this_thread::sleep_for(std::chrono::milliseconds(100));
            }
        }
    }
}
```

`std::lock_guard` 在离开作用域时释放 mutex；这里 `lock_desc` 活到 descriptor map 和 type map 两段发布循环结束，`lock_type` 则只在后半段持有。于是注册 callback 若要修改 descriptor map，会等整个 descriptor 发布循环和其中的 sleep；更新 type map 时则可能等更短或更长的 type 循环。调用 `publish()` 本身也在相应 map 锁内，慢调用会把等待传给更新线程。

Linux 上，`std::thread` 通常由 pthread 实现成可被内核调度的线程；它与构造线程是两个独立的可运行执行流。`sleep_for(100ms)` 到期前会阻塞当前 worker，计时器到期只让它重新成为 runnable；内核还需选择它运行，循环重新检查 atomic 后函数才能返回。析构线程调用 `join()` 时也会阻塞，直到 worker 真正结束。因而 `mqtt_desc_thread_active = false` 只是改变共享停止状态，不是立即唤醒正在 sleep 的线程；轮询实现额外带来最多约 100 ms 的等待粒度。

安全形状是所有普通成员先构造，`initialize()` 全部成功后再启动 `std::jthread`：

**教学/复刻示例（不是固定提交源码摘录）：**

```cpp
class BrokerBridge {
  std::atomic<bool> accepting_{false};
  std::jthread metadata_worker_;  // 最后启动，析构时自动 request_stop + join

  bool Start() {
    if (!CreateEcalEntities() || !ConnectMqtt()) return false;
    accepting_.store(true, std::memory_order_release);
    metadata_worker_ = std::jthread(
        [this](std::stop_token stop) { MetadataLoop(stop); });
    return true;
  }
};
```

`jthread` 解决 join 所有权，但 callback 仍要通过 accepting/in-flight 计数或库的撤销保证，不可仅依赖原子 bool 防止已经进入的 callback 使用正在析构的 map。

关闭顺序应先撤销 eCAL/MQTT 的新 callback，关闭队列入口，再让 worker 处理完或按策略取消剩余项，最后停止 MQTT loop 和 eCAL runtime。若先 join worker、后关闭入口，callback 仍可能继续入队，join 永远等不到稳定空状态。

broker 断线时不能反复无界创建重连任务。需要单一连接状态机、带抖动的指数退避、最大积压年龄和可观测的 disconnected duration。重连成功后也要重新建立 MQTT subscription，并决定断线期间积压是重放还是丢弃。

最后把源码实际析构顺序摊开。它先停止并 join descriptor worker，再停止 MQTT loop，随后清理 MQTT 全局库、逐个 delete eCAL 实体，最后 Finalize eCAL：


```cpp
Bridge::~Bridge(void)
{
    mqtt_desc_thread_active = false;
    is_initialized = false;
    mqtt_desc_thread.join();
    disconnect();
    is_connected_to_mqtt_broker = false;
    loop_stop(true);
    mosqpp::lib_cleanup();
    for (auto const& it_publisher : ecal_publishers)
    {
        delete it_publisher.second;
    }

    for (eCAL::CSubscriber* subscriber : ecal_subscribers)
    {
        delete subscriber;
    }
    eCAL::Finalize();
}
```

`join()` 是这段代码唯一显式等待 Bridge 自建 descriptor worker 退出的屏障；`disconnect()`、`loop_stop(true)` 与 `lib_cleanup()` 属于 Mosquitto 侧，而 publisher/subscriber 的 `delete` 和 `Finalize()` 属于 eCAL 侧。Bridge 在析构里没有显式撤销并等待 eCAL callback 的步骤，所以不能从 `join()` 推出 eCAL callback 已退出。

这里必须把桥接程序和 eCAL 库分开核验。桥接仓库说明它至少需要 eCAL 5.11.0，而固定提交的 CMake 声明只按包名查找依赖：


```cmake
find_package(eCAL REQUIRED)
```

这行没有版本约束或 `EXACT`，所以桥接 commit 不能单独说明构建时选中了哪个 eCAL 补丁版。`delete CSubscriber*` 的内部同步语义也不能只靠桥接源码证明。为了看清 API 代际差别，下面对照另一个固定 eCAL commit：它同时包含 v6 公共 API 和 v5 兼容层。v6 `CSubscriber` 析构只从 SubGate 注销：


```cpp
CSubscriber::~CSubscriber()
{
  auto subscriber_impl = m_subscriber_impl.lock();

  // unregister subscriber
  auto subgate = g_subgate();
  if (subgate && subscriber_impl) subgate->Unregister(subscriber_impl->GetTopicName(), subscriber_impl);
}
```

同一 commit 的 v5 兼容 façade 走另一条销毁路径：它先移除 receive callback，再从 SubGate 注销 reader，最后释放实现对象：

接着看 `CSubscriber::Destroy` 的真实实现：

```cpp
bool CSubscriber::Destroy()
{
  if (m_subscriber_impl == nullptr) return(false);

  // remove receive callback
  RemReceiveCallback();

  // unregister datareader
  auto subgate = g_subgate();
  if (subgate) subgate->Unregister(m_subscriber_impl->GetTopicName(), m_subscriber_impl);

  // destroy datareader
  m_subscriber_impl.reset();
  return(true);
}
```

这两段不能合并成一句“删除 Subscriber 一定等待 callback”：它们属于同一 eCAL commit 中的不同 API 代际，而桥接仓库没有固定链接到该 commit。下文的锁分析准确描述这个 eCAL commit 的共享实现与 v5 兼容路径；部署时仍要把所用 eCAL 精确版本纳入构建基线，确认它的 `Destroy`/析构实际走哪条路径。

在该 eCAL commit 中，`CSubGate::ApplySample` 会先把匹配的 `shared_ptr<CSubscriberImpl>` 复制到局部 vector、释放 Gate 锁，再调用实现对象；Gate 注销不能撤销已经取得强引用的那次调用。`CSubscriberImpl::ApplySample` 则从函数入口持有 `m_receive_callback_mutex` 到用户 callback 返回。v5 `RemReceiveCallback()` 最终清空的正是这把锁保护的函数对象：

接着看 `CSubscriberImpl::ApplySample` 的真实实现：

```cpp
size_t CSubscriberImpl::ApplySample(const Payload::TopicInfo& topic_info_, const char* payload_, size_t size_, long long id_, long long clock_, long long time_, size_t /*hash_*/, eTLayerType layer_)
{
  // ensure thread safety
  const std::lock_guard<std::mutex> lock(m_receive_callback_mutex);
  if (!m_created) return(0);

  // We don't want to apply samples which are received on layers which are not activated for this subscriber
  if (!ShouldApplySampleBasedOnLayer(layer_))
  {
    return 0;
  }

  auto publication_info = PublicationInfoFromTopicInfo(topic_info_);

  // We do not want to apply duplicate / old samples
  if (!ShouldApplySampleBasedOnClock(publication_info, clock_))
  {
    // not clear why we are returning the size_ if we are not applying the sample, but why not...
    return size_;
  }

  // We might not want to apply samples sent with a given ID (deprecated!)
  if (!ShouldApplySampleBasedOnId(id_))
  {
    return 0;
  }

  // store receive layer
  m_layers.udp.active |= layer_ == tl_ecal_udp;
  m_layers.shm.active |= layer_ == tl_ecal_shm;
  m_layers.tcp.active |= layer_ == tl_ecal_tcp;

#ifndef NDEBUG
  // log it
  eCAL::Logging::Log(Logging::log_level_debug3, m_attributes.topic_name + "::CSubscriberImpl::ApplySample");
#endif

  // increase read clock
  m_clock++;

  TriggerMessageDropUdate(publication_info, clock_);
  TriggerStatisticsUpdate(time_);

  // reset timeout
  m_receive_time = 0;

  // store size
  m_topic_size = size_;

  // execute callback
  bool processed = false;
  {
    // call user receive callback function
    if(m_receive_callback)
    {
#ifndef NDEBUG
      // log it
      eCAL::Logging::Log(Logging::log_level_debug3, m_attributes.topic_name + "::CSubscriberImpl::ApplySample::ReceiveCallback");
#endif
      // prepare data struct
      SReceiveCallbackData cb_data;
      cb_data.buffer   = static_cast<const void*>(payload_);
      cb_data.buffer_size  = size_;
      cb_data.send_timestamp  = time_;
      cb_data.send_clock = clock_;

      STopicId topic_id;
      topic_id.topic_name          = topic_info_.topic_name;
      topic_id.topic_id.host_name  = topic_info_.host_name;
      topic_id.topic_id.entity_id  = topic_info_.topic_id;
      topic_id.topic_id.process_id = topic_info_.process_id;

      SPublicationInfo pub_info;
      pub_info.entity_id  = topic_info_.topic_id;
      pub_info.host_name  = topic_info_.host_name;
      pub_info.process_id = topic_info_.process_id;

      // execute it
      const std::lock_guard<std::mutex> exec_lock(m_connection_map_mtx);
      (m_receive_callback)(topic_id, m_connection_map[pub_info].data_type_info, cb_data);
      processed = true;
    }
  }

  // if not consumed by user receive call
  if (!processed)
  {
    // push sample into read buffer
    const std::lock_guard<std::mutex> read_buffer_lock(m_read_buf_mutex);
    m_read_buf.clear();
    m_read_buf.assign(payload_, payload_ + size_);
    m_read_time = time_;
    m_read_buf_received = true;

    // inform receive
    m_read_buf_cv.notify_one();
  }

  return(size_);
}
```

源码表明 callback 使用的是 `payload_` 借用指针：`SReceiveCallbackData` 只把该地址与长度交给用户，不复制 payload；同步 callback 返回前它仍有效。更关键的是最外层 `lock` 在整个函数返回时才析构，callback 分支还会嵌套取得 `m_connection_map_mtx`。因此由另一个线程执行的 `RemoveReceiveCallback()` 要先等当前执行退出，再清空回调；后续样本拿到锁时会读到空函数对象。


```cpp
bool CSubscriber::RemReceiveCallback()
{
  if (m_subscriber_impl == nullptr) return(false);
  return(m_subscriber_impl->RemoveReceiveCallback());
}

bool CSubscriberImpl::RemoveReceiveCallback()
{
  if (!m_created) return(false);

#ifndef NDEBUG
  eCAL::Logging::Log(Logging::log_level_debug2, m_attributes.topic_name + "::CSubscriberImpl::RemoveReceiveCallback");
#endif

  // remove receive callback
  {
    const std::lock_guard<std::mutex> lock(m_receive_callback_mutex);
    m_receive_callback = nullptr;
  }

  return(true);
}
```

`ApplySample` 持这把 mutex 执行用户函数，所以在这份 eCAL 实现中，由 owner 线程调用 v5 `RemReceiveCallback()` 会等当前 callback 退出，再把函数对象清空；之后到达的样本进入 `ApplySample` 时看到空 callback，会转入 read-buffer 分支，而不会再调用 Bridge。它只为 receive callback 建立串行化边界；不能在 callback 自身中调用，否则会再次锁住当前线程已经持有的非递归 mutex。这个结论属于所摘录的 eCAL commit，不应外推成桥接 README 所允许的每个 eCAL 版本都有相同保证。

将 Bridge 自身的关闭顺序与上述 eCAL 实现合起来看：eCAL reader 已在 `ecalMessageReceived()` 入口读到 `is_initialized == true` 和 connected；主线程开始析构，将 initialized 设为 false，接着停止 MQTT loop 并执行 `lib_cleanup()`；已经通过入口判断的 callback 仍可能继续走到 `publish()`。即使某个 eCAL 版本会在删除 Subscriber 时等待 callback，这个等待也发生在 Bridge 已经清理 Mosquitto 之后，时序仍然太晚。改进版本应先关闭入口，并在清理 MQTT client 前显式撤销每个 receive callback、等待已进入 callback 返回，再停止/清理 MQTT，最后销毁 eCAL 实体与进程级 runtime；registration callback 也要单独核实撤销语义。这个屏障会使关闭等待 callback 的剩余执行时间，因此 callback 的 WCET、阻塞 I/O 和超时策略必须进入关闭时限预算。

### 当前重连与多 broker 控制流

`on_disconnect()` 只把 connected atomic 置 false。主循环每两秒检查状态，且仅在收发计数都为零时调用 `tryReconnectMqtt()`；一旦连接曾经交换过数据，计数非零，断线后可能永远不进入该重连分支。连接状态与“历史上是否收过消息”是两件不同的状态，不应互相作为门控。

外层 `run()` 还对 `list_of_bridges` 逐个遍历，但在第一个 initialized Bridge 上进入 `while (eCAL::Ok())`，正常运行期间永远不会检查第二个 Bridge 的健康状态。构造阶段仍创建了所有 Bridge，监控循环却被第一个阻塞。多 broker 应使用一个统一 supervisor 每轮遍历全部 sessions，或每个 session 自有状态机并向 supervisor 汇总事件。

一个清晰的 broker 状态机至少包含：

```text
Stopped -> Connecting -> Online
              |           |
              v           v
           Backoff <- Disconnected
              |
              +-- deadline + jitter --> Connecting

任意状态 -- shutdown --> Draining -> Stopped
```

每次成功 `on_connect` 重新订阅全部 routes；每次失败推进 attempt 并计算有上限的指数退避。是否保留离线期间 payload、最大年龄与容量应是 route policy，而不是由 Mosquitto 内部队列偶然决定。

## C++ 工程能力

该类应用需要两个第三方 callback API 共存。推荐把每套库封装为 owner class，以 `std::jthread`/stop token 或明确 condition variable 管理 worker；callback 捕获 `weak_ptr<State>`，关闭时先撤销入口、停止队列、等待线程，再 Finalize eCAL 和 MQTT client。

### 原子状态与普通计数器不能混用

源码将 initialized、connected、loop_started 和 worker active 声明为 `std::atomic<bool>`，这是因为 Mosquitto loop、eCAL callback、descriptor worker 与主线程都会观察状态。但 `mqtt_rx_counter`、`ecal_rx_counter` 仍是普通 `int`：callback 递增，主线程读取和重连时清零，构成 data race。

若计数只用于指标，可用 `std::atomic<std::uint64_t>::fetch_add(1, std::memory_order_relaxed)`；relaxed 足以保证计数本身不撕裂，不借它发布其他数据。若“计数清零”承担状态机语义，原子计数仍不够，应把连接 generation 与统计窗口放入 supervisor 的单写状态。

`loop_started` 虽是 atomic，但 `if (!loop_started) { loop_start(); loop_started=true; }` 是 check-then-act；多个调用线程仍可能同时启动。当前主路径大多串行，并不等于类接口本身线程安全。可用 `compare_exchange` 取得启动权，或更简单地规定 Start 只能由 owner 线程调用并用状态 enum 保护。

### 回调不应线性扫描配置

`ecalMessageReceived()` 对每条消息遍历 `ecal2mqtt_topics`；`on_message()` 又遍历全部 MQTT routes，并为比较反复构造 `std::string(message->topic)`。映射数 `N`、消息率 `R` 时，仅路由选择成本就是 `O(RN)`，还包含临时分配。

启动阶段可建立：

**教学/复刻示例（不是固定提交源码摘录）：**

```cpp
struct Route {
  std::string source;
  std::string target;
  int qos{};
  bool retain{};
  eCAL::CPublisher* ecal_publisher{};  // owner 在 Bridge 中，Route 只借用
};

std::unordered_map<std::string, RouteId> ecal_payload_routes;
std::unordered_map<std::string, RouteId> mqtt_payload_routes;
std::unordered_map<std::string, MetadataRoute> mqtt_metadata_routes;
```

callback 以 `std::string_view` 先查表，命中后才复制需要长期保存的数据。Route 表在 Start 后不可变，callback 无需锁；publisher owner 在停止 callback 并等待 in-flight 归零后才销毁。

### 多个全局库需要进程 owner

每个 `Bridge` 调用 `eCAL::Initialize/Finalize` 与 `mosqpp::lib_init/lib_cleanup`，但这些 API 属于进程级。更清晰的对象层次是：

```text
ApplicationRuntime
  |-- EcalRuntime (exactly once)
  |-- MosquittoLibrary (exactly once)
  `-- vector<BrokerSession>
        `-- routes + client loop + workers
```

`ApplicationRuntime` 最先构造、最后析构；BrokerSession 不允许触碰全局 finalize。这样一个 broker 初始化失败只回滚自己的实体，不会关闭其他 broker 仍在使用的运行时。

## 取舍与不足

桥接提高互操作，却增加一次排队、可能的重编码和新的故障域。MQTT QoS 不能自动升级 eCAL 源端可靠性；eCAL 已丢失的数据无法由 broker 找回。反过来，MQTT 重投可能让 eCAL 消费者看到重复数据，因此业务需要 message id 与幂等处理。

仓库值得保留的设计包括：配置明确分开两个方向；一 broker 一 client 便于隔离连接参数；type/descriptor 不被假装成 payload 自带能力；使用 eCAL registration event 获取真实 publisher metadata；Mosquitto 的连接回调中重新订阅 routes；TLS、证书和 PSK 作为部署参数暴露。

源码级不足可以按影响分类：

| 类别 | 固定提交表现 | 可能后果 | 改进方向 |
|---|---|---|---|
| 校验 | QoS 范围用不可能成立的 `&&` | 非法 QoS 延后到库调用 | schema + 明确 sentinel + `||` |
| 值语义 | range-for 修改 map 副本 | broker 默认值没有真正写回 | 结构化绑定引用 |
| 构造并发 | 构造期间启动捕获 `this` 的线程 | 读取未构造成员、半对象逃逸 | 两阶段 Start + `jthread` |
| 回调路由 | 每消息线性扫描所有 mappings | 映射数增加时 CPU/分配上升 | 启动期不可变哈希索引 |
| payload 回压 | callback 直接跨库 publish/send | 慢路径占用 middleware callback | 有界 mailbox + worker |
| 指标 | 普通 `int` 跨线程读写 | C++ data race | relaxed atomic 或单写指标汇总 |
| 多 broker | 第一个 Bridge 的 while 阻塞 supervisor | 其他 broker 无健康/重连检查 | 统一 supervisor 状态机 |
| 全局生命周期 | 每 Bridge init/finalize 全局库 | session 相互影响 | 进程级 runtime owner |
| metadata worker | 持锁 publish 并 sleep | registration callback 等锁 | 锁内快照、锁外发送 |
| wire 语义 | payload 与 schema 分 topic，无版本关联 | schema 更新时错配 | versioned envelope/schema id |

性能上，当前 payload 路径没有应用级额外队列复制，但有线性 route 查找；descriptor 路径保存 `std::string` 副本并周期重发。改成有界队列会增加一次 payload copy 与排队，却能限制 callback 占用时间。选择应由消息大小 `S`、速率 `R`、route 数 `N` 和离线容忍时间 `D` 定量决定：离线全缓存近似需要 `R × S × D` 字节，通常不能无界承诺。

该仓库本身规模很小，README 还明确说明因 Mosquitto 在 Windows 上的线程支持问题而只保留 Linux 支持。它适合学习透明桥接和做受控部署起点，但不能仅凭示例代码就推断出高可用、跨平台或安全认证已经完整。工业化版本还需补配置 schema/version、TLS secret 管理、健康检查、指标、限流、故障注入和滚动升级兼容策略。

## 从案例发展成自己的桥接器

实现顺序可以固定为：先只做单向单 topic 和静态 type；加入有界队列与 drop 指标；再支持多映射；随后增加 type/descriptor side channel；最后才做双向、热重载和重连。每一步都用断 broker、慢消费者、重复消息、超大 payload 和退出竞态验证对应语义。

### 第一步：定义配置模型和不变量

不要让 YAML node 进入 callback。先解析成强类型配置，再执行一次全局 validation：

**教学/复刻示例（不是固定提交源码摘录）：**

```cpp
enum class Direction { ecal_to_mqtt, mqtt_to_ecal };
enum class Overflow { keep_latest, drop_newest, reject_source };

struct RouteConfig {
  std::string name;
  Direction direction;
  std::string source;
  std::string target;
  std::optional<std::string> static_type;
  int mqtt_qos{};
  bool retain{};
  std::size_t max_payload_bytes{};
  std::size_t queue_capacity{};
  Overflow overflow{};
};
```

校验必须覆盖唯一 route name、存在的 broker、QoS 0..2、容量非零、payload 上限、topic 冲突、static/dynamic type 策略互斥、证书文件和 secret 来源。配置校验成功后生成索引，运行期不再解释 YAML 字符串。

### 第二步：建立进程运行时和单 broker session

```text
main
  -> EcalRuntime owner
  -> MosquittoLibrary owner
  -> BrokerSession::Create(validated config)
       -> create MQTT client but do not accept callbacks
       -> create eCAL publishers/subscribers into local unique_ptrs
       -> build immutable route tables
       -> connect broker and subscribe
       -> start workers
       -> publish accepting=true
```

`Create` 返回 `expected<unique_ptr<BrokerSession>, StartupError>` 一类结果，比构造函数内部启动线程更容易报告阶段性错误。所有实体先放局部 RAII 容器，最后移动到 session 成员，形成提交点。

### 第三步：实现两个方向的有界 mailbox

状态流可使用每 route 单槽 latest-value mailbox；事件流使用固定容量 ring，并定义满时行为。Envelope 拥有 payload：

**教学/复刻示例（不是固定提交源码摘录）：**

```cpp
struct Envelope {
  RouteId route;
  std::vector<std::byte> payload;
  std::uint64_t sequence;
  std::chrono::steady_clock::time_point enqueued_at;
};
```

callback 只做长度检查、拥有化复制、`try_push` 和原子指标。worker 取出后调用另一中间件。大消息吞吐不足时，可引入固定块内存池或 shared buffer，但必须证明 buffer 的释放线程和最长持有时间，不能把 callback 裸指针跨线程保存。

### 第四步：把 schema 与 payload 绑定

静态类型 route 在启动时设置 eCAL publisher type；动态类型 route 维护 `schema_generation`。只有拿到 generation 对应的 type/descriptor 后才释放该 generation 的 payload。若 schema 变化，先创建/更新 metadata，再切换 active generation；远端用同一 generation 拒绝错配。

### 第五步：实现 supervisor 与关闭协议

一个 supervisor 管理所有 BrokerSession 的 Connecting/Online/Backoff/Draining 状态，使用单调时钟 deadline 和随机抖动。关闭时：

```text
accepting=false
  -> unsubscribe/remove eCAL callbacks
  -> stop new MQTT callback dispatch
  -> drain or cancel bounded queues according to policy
  -> request_stop + join workers
  -> disconnect/stop client loops
  -> destroy publishers/subscribers
  -> application runtime finalize
```

每一步都应有超时和幂等标记，使部分启动失败也能调用同一 Stop。多 broker 不共享 session 状态，但共享的 eCAL/Mosquitto global owner 只在全部 session 销毁后 finalize。

完成标准不是“两个终端都看见消息”，而是能够回答队列上界、断线期间的行为、每类消息的端到端语义、关闭最长时间以及 schema 不兼容时如何拒绝。

## 可迁移设计

所有中间件桥都应先写“语义矩阵”：命名、类型、顺序、重复、保留、背压、发现、认证和关闭分别如何映射。只有每格都有明确策略，双向 publish 才是可靠系统而不是演示程序。
