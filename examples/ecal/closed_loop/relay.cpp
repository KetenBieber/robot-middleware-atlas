#include "wire_codec.h"

#include <ecal/ecal.h>
#include <ecal/pubsub/publisher.h>
#include <ecal/pubsub/subscriber.h>

#include <atomic>
#include <chrono>
#include <iostream>
#include <string>
#include <string_view>
#include <thread>

namespace {

eCAL::SDataTypeInformation FrameType() {
  return {"atlas.ecal.Frame", "text", "seq|timestamp_us|payload"};
}

bool WaitForSubscriber(eCAL::CPublisher& publisher,
                       std::chrono::milliseconds timeout) {
  const auto deadline = std::chrono::steady_clock::now() + timeout;
  while (eCAL::Ok() && std::chrono::steady_clock::now() < deadline) {
    if (publisher.GetSubscriberCount() > 0) {
      return true;
    }
    std::this_thread::sleep_for(std::chrono::milliseconds(50));
  }
  return false;
}

}  // namespace

int main() {
  if (!eCAL::Initialize("atlas_ecal_relay")) {
    return 1;
  }

  int result = 0;
  {
    eCAL::CPublisher processed("/atlas/processed", FrameType());
    if (!WaitForSubscriber(processed, std::chrono::seconds(5))) {
      std::cerr << "no observer subscriber discovered\n";
      result = 2;
    } else {
      std::atomic<int> forwarded{0};
      std::atomic<int> invalid{0};
      std::atomic<int> send_failures{0};

      eCAL::CSubscriber raw("/atlas/raw", FrameType());
      raw.SetReceiveCallback(
          [&](const eCAL::STopicId&, const eCAL::SDataTypeInformation&,
              const eCAL::SReceiveCallbackData& data) {
            const auto* bytes = static_cast<const char*>(data.buffer);
            const std::string_view wire(bytes, data.buffer_size);
            auto frame = atlas::ecal_demo::Decode(wire);
            if (!frame) {
              invalid.fetch_add(1);
              return;
            }

            frame->payload = "processed:" + frame->payload;
            const std::string output = atlas::ecal_demo::Encode(*frame);
            if (!processed.Send(output)) {
              send_failures.fetch_add(1);
              return;
            }
            forwarded.fetch_add(1);
          });

      const auto deadline =
          std::chrono::steady_clock::now() + std::chrono::seconds(8);
      while (eCAL::Ok() && forwarded.load() < 20 &&
             std::chrono::steady_clock::now() < deadline) {
        std::this_thread::sleep_for(std::chrono::milliseconds(50));
      }

      raw.RemoveReceiveCallback();
      std::cout << "forwarded=" << forwarded.load()
                << " invalid=" << invalid.load()
                << " send_failures=" << send_failures.load() << '\n';

      if (invalid.load() != 0 || send_failures.load() != 0) {
        result = 3;
      } else if (forwarded.load() != 20) {
        result = 4;
      }
    }
  }

  eCAL::Finalize();
  return result;
}
