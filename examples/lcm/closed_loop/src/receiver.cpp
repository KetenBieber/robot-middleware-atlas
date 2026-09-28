#include <lcm/lcm-cpp.hpp>
#include "atlas/joint_state_t.hpp"
#include <chrono>
#include <cstdint>
#include <iostream>
#include <string>

class Handler {
public:
    void onState(const lcm::ReceiveBuffer* raw, const std::string& channel,
                 const atlas::joint_state_t* msg) {
        if (msg->joint_count != static_cast<int32_t>(msg->joints.size()) ||
            msg->joint_count != 3) {
            std::cerr << "Rejected inconsistent joint_count\n";
            ++invalid;
            return;
        }
        if (last_seq >= 0 && msg->sequence != last_seq + 1) {
            std::cerr << "gap/reorder: previous=" << last_seq
                      << " new=" << msg->sequence << '\n';
            ++gaps;
        }
        last_seq = msg->sequence;
        ++received;
        std::cout << channel << " seq=" << msg->sequence
                  << " raw_bytes=" << raw->data_size
                  << " joint0=" << msg->joints[0] << '\n';
        // raw->data 和 msg 都只借用本次回调的内存；
        // 异步转交之前必须复制为业务自己拥有的消息。
    }
    int received{};
    int invalid{};
    int gaps{};
    int64_t last_seq{-1};
};

int main(int argc, char** argv) {
    const std::string url =
        argc > 1 ? argv[1] : "udpm://239.255.76.67:7667?ttl=0";
    // Handler 先构造、后析构，确保整个 LCM 订阅生命周期内对象存活。
    Handler handler;
    lcm::LCM bus(url);
    if (!bus.good()) {
        std::cerr << "Failed to create LCM provider: " << url << '\n';
        return 1;
    }
    auto* sub = bus.subscribe("ATLAS_JOINT_STATE", &Handler::onState, &handler);
    if (!sub || sub->setQueueCapacity(4) < 0) {
        std::cerr << "subscribe/configuration failed\n";
        return 2;
    }
    const auto deadline = std::chrono::steady_clock::now() +
                          std::chrono::seconds(12);
    while (handler.received < 20 && std::chrono::steady_clock::now() < deadline) {
        const int rc = bus.handleTimeout(250);
        if (rc < 0) {
            std::cerr << "handle failed\n";
            return 3;
        }
    }
    std::cout << "received=" << handler.received
              << " gaps=" << handler.gaps
              << " invalid=" << handler.invalid << '\n';
    bus.unsubscribe(sub);
    if (handler.invalid != 0)
        return 4;
    if (handler.received != 20 || handler.gaps != 0) {
        // 完整交付验收：UDP 本身仍然不保证可靠交付。
        std::cerr << "Incomplete delivery: expected 20 ordered messages\n";
        return 5;
    }
    return 0;
}
