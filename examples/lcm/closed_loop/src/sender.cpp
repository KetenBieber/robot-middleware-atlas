#include <lcm/lcm-cpp.hpp>
#include "atlas/joint_state_t.hpp"
#include <chrono>
#include <cstdint>
#include <iostream>
#include <string>
#include <thread>

int main(int argc, char** argv) {
    const std::string url =
        argc > 1 ? argv[1] : "udpm://239.255.76.67:7667?ttl=0";
    lcm::LCM bus(url);
    if (!bus.good()) {
        std::cerr << "Failed to create LCM provider: " << url << '\n';
        return 1;
    }
    // 先启动接收端：UDPM 不为尚未在线的订阅者保存历史。
    for (int64_t seq = 0; seq < 20; ++seq) {
        atlas::joint_state_t state;
        state.timestamp_us = std::chrono::duration_cast<std::chrono::microseconds>(
            std::chrono::system_clock::now().time_since_epoch()).count();
        state.sequence = seq;
        state.joints = {0.01 * static_cast<double>(seq),
                        0.02 * static_cast<double>(seq),
                        -0.01 * static_cast<double>(seq)};
        state.joint_count = static_cast<int32_t>(state.joints.size());
        const int rc = bus.publish("ATLAS_JOINT_STATE", &state);
        if (rc < 0) {
            std::cerr << "Local publish failed at seq=" << seq << '\n';
            return 2;
        }
        std::cout << "publish seq=" << seq
                  << " bytes=" << state.getEncodedSize() << '\n';
        std::this_thread::sleep_for(std::chrono::milliseconds(50));
    }
    // 本地 publish 成功不是订阅者的交付确认。
    return 0;
}
