#include "wire_codec.h"

#include <ecal/ecal.h>
#include <ecal/pubsub/subscriber.h>

#include <atomic>
#include <chrono>
#include <cstdint>
#include <iostream>
#include <string_view>
#include <thread>

namespace {

eCAL::SDataTypeInformation FrameType() {
  return {"atlas.ecal.Frame", "text", "seq|timestamp_us|payload"};
}

}  // namespace

int main() {
  if (!eCAL::Initialize("atlas_ecal_observer")) {
    return 1;
  }

  int result = 0;
  {
    std::atomic<int> received{0};
    std::atomic<int> invalid{0};
    std::atomic<int> gaps{0};
    std::atomic<std::int64_t> last_sequence{-1};

    eCAL::CSubscriber subscriber("/atlas/processed", FrameType());
    subscriber.SetReceiveCallback(
        [&](const eCAL::STopicId&, const eCAL::SDataTypeInformation&,
            const eCAL::SReceiveCallbackData& data) {
          const auto* bytes = static_cast<const char*>(data.buffer);
          const std::string_view wire(bytes, data.buffer_size);
          const auto frame = atlas::ecal_demo::Decode(wire);
          if (!frame || frame->payload != "processed:raw") {
            invalid.fetch_add(1);
            return;
          }

          const auto sequence = static_cast<std::int64_t>(frame->sequence);
          const auto previous = last_sequence.exchange(sequence);
          if (previous >= 0 && sequence != previous + 1) {
            gaps.fetch_add(1);
          }
          received.fetch_add(1);
          std::cout << "observer seq=" << sequence << '\n';
        });

    const auto deadline =
        std::chrono::steady_clock::now() + std::chrono::seconds(10);
    while (eCAL::Ok() && received.load() < 20 &&
           std::chrono::steady_clock::now() < deadline) {
      std::this_thread::sleep_for(std::chrono::milliseconds(50));
    }

    subscriber.RemoveReceiveCallback();
    std::cout << "received=" << received.load()
              << " gaps=" << gaps.load()
              << " invalid=" << invalid.load() << '\n';

    if (invalid.load() != 0) {
      result = 2;
    } else if (received.load() != 20 || gaps.load() != 0) {
      result = 3;
    }
  }

  eCAL::Finalize();
  return result;
}
