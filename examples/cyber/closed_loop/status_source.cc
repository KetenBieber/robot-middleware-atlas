#include <cstdint>
#include <memory>

#include "cyber/cyber.h"
#include "cyber/examples/atlas_closed_loop/status.pb.h"
#include "cyber/time/rate.h"
#include "cyber/time/time.h"

int main(int argc, char** argv) {
  if (!apollo::cyber::Init(argv[0])) {
    return 1;
  }
  auto node = apollo::cyber::CreateNode("atlas_status_source");
  if (!node) {
    return 2;
  }
  auto writer = node->CreateWriter<atlas::cyber_demo::Status>(
      "/atlas/status/raw");
  if (!writer) {
    return 3;
  }

  apollo::cyber::Rate rate(20.0);
  for (std::uint64_t sequence = 0;
       sequence < 20 && apollo::cyber::OK(); ++sequence) {
    auto message = writer->AcquireMessage();
    if (!message) {
      return 4;
    }
    message->set_sequence(sequence);
    message->set_timestamp_ns(apollo::cyber::Time::Now().ToNanosecond());
    message->set_text("raw");
    if (!writer->Write(message)) {
      return 5;
    }
    AINFO << "source seq=" << sequence;
    rate.Sleep();
  }
  return 0;
}
