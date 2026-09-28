#include "wire_codec.h"

#include <ecal/ecal.h>
#include <ecal/pubsub/publisher.h>
#include <ecal/time.h>

#include <chrono>
#include <cstdint>
#include <iostream>
#include <string>
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
  if (!eCAL::Initialize("atlas_ecal_source")) {
    return 1;
  }

  int result = 0;
  {
    eCAL::CPublisher publisher("/atlas/raw", FrameType());
    if (!WaitForSubscriber(publisher, std::chrono::seconds(5))) {
      std::cerr << "no relay subscriber discovered\n";
      result = 2;
    } else {
      for (std::uint64_t sequence = 0; sequence < 20 && eCAL::Ok();
           ++sequence) {
        atlas::ecal_demo::Frame frame;
        frame.sequence = sequence;
        frame.timestamp_us = eCAL::Time::GetMicroSeconds();
        frame.payload = "raw";

        const std::string wire = atlas::ecal_demo::Encode(frame);
        if (!publisher.Send(wire)) {
          std::cerr << "send failed at seq=" << sequence << '\n';
          result = 3;
          break;
        }
        std::cout << "source seq=" << sequence << '\n';
        std::this_thread::sleep_for(std::chrono::milliseconds(50));
      }
    }
  }

  eCAL::Finalize();
  return result;
}
