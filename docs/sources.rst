固定版本源码索引
================

固定版本
--------

.. list-table::
   :header-rows: 1

   * - 项目
     - Commit
     - 官方仓库
   * - Apollo Cyber RT
     - ``d53aa3da47a06a08e6d0cd175d5623a34fa0d6aa``
     - https://github.com/ApolloAuto/apollo
   * - Orocos RTT
     - ``600102e8be9c81905b20930e32d43b28244ab173``
     - https://github.com/orocos-toolchain/rtt
   * - YARP
     - ``91710eb45baf5d9cb62dd5a0cb3c3a00f42481b9``
     - https://github.com/robotology/yarp
   * - Eclipse eCAL
     - ``1ec0ea2fe5e5e61e3e492be6128c27cc6026d717``
     - https://github.com/eclipse-ecal/ecal
   * - Eclipse Zenoh
     - ``9fcd9cb5d364192c3e8a27e66de76f4bc750d1d5``
     - https://github.com/eclipse-zenoh/zenoh
   * - LCM
     - ``ad0c54cee0ec048ef12357c34349ec1443158864``
     - https://github.com/lcm-proj/lcm
   * - IgH EtherCAT Master
     - ``61cc654f5b721ddd54df0f58bdd34106d91c5359`` （stable-1.6 / 1.6.13）
     - https://gitlab.com/etherlab.org/ethercat
   * - SOEM
     - ``304d1c05eab77dc0d426f1a5cf09c8cc7dc03713`` （v2.0.0）
     - https://github.com/OpenEtherCATsociety/SOEM
   * - Eclipse Cyclone DDS
     - ``e54e991f75a3e67f8e628da3171122e36ea5b872`` （11.0.1）
     - https://github.com/eclipse-cyclonedds/cyclonedds
   * - eProsima Fast DDS
     - ``39303846fb8534ef69fa65f9fa4bcc9e6a7c995a`` （v3.6.2）
     - https://github.com/eProsima/Fast-DDS
   * - Eclipse iceoryx2
     - ``135d09dd8b29f321f1725920d434864c4e512378`` （v0.10.0）
     - https://github.com/eclipse-iceoryx/iceoryx2

阅读方式
--------

正文中的源码片段用于解释局部控制流和数据结构，固定提交 permalink 用于查看完整上下文。
同名类型或函数在不同版本中可能已经变化，因此跨章节对照时应先核对本页列出的提交。
IgH EtherCAT Master 已固定到本地 ``stable-1.6`` 的 1.6.13 提交；SOEM 已固定到 ``v2.0.0`` 的 ``304d1c05`` 提交。两个 EtherCAT 主站专题后续源码片段都统一从对应固定提交核对。
Cyclone DDS 已固定到 ``11.0.1`` 的 ``e54e991f``；ROS 2 集成案例另固定 ``rmw_cyclonedds`` 4.2.1 的 ``19478b0``，用于核对 RMW 到 DDS 的真实映射，不替代 Cyclone DDS 本体源码基线。
Fast DDS 已固定到 ``v3.6.2`` 的 ``39303846``；ROS 2 集成案例另固定 ``rmw_fastrtps`` 的 ``a88ce42``，用于核对 ROS QoS、publish 与 waitset 到 Fast DDS 的真实适配链。
iceoryx2 已固定到 ``v0.10.0`` 的 ``135d09dd``；本专题以 Node/Service、SharedMemory/DataSegment、PointerOffset/ZeroCopyConnection、WaitSet/Reactor 与 dead-node cleanup 为主要源码真值。
