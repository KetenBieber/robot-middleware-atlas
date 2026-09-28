#include <atomic>
#include <chrono>
#include <cstdint>
#include <memory>
#include <string>
#include <thread>

#include "cyber/cyber.h"
#include "cyber/examples/atlas_closed_loop/status.pb.h"

class Observer {
 public:
  void OnStatus(const std::shared_ptr<atlas::cyber_demo::Status>& message) {
    const std::int64_t sequence =
        static_cast<std::int64_t>(message->sequence());
    const std::int64_t previous = last_sequence_.exchange(sequence);
    if (previous >= 0 && sequence != previous + 1) {
      gaps_.fetch_add(1);
    }
    if (message->text().rfind("processed:", 0) != 0) {
      invalid_.fetch_add(1);
    }
    received_.fetch_add(1);
    AINFO << "observer seq=" << sequence << " text=" << message->text();
  }

  int received() const { return received_.load(); }
  int gaps() const { return gaps_.load(); }
  int invalid() const { return invalid_.load(); }

 private:
  std::atomic<int> received_{0};
  std::atomic<int> gaps_{0};
  std::atomic<int> invalid_{0};
  std::atomic<std::int64_t> last_sequence_{-1};
};

int main(int argc, char** argv) {
  if (!apollo::cyber::Init(argv[0])) {
    return 1;
  }
  auto node = apollo::cyber::CreateNode("atlas_status_observer");
  if (!node) {
    return 2;
  }

  Observer observer;
  auto reader = node->CreateReader<atlas::cyber_demo::Status>(
      "/atlas/status/processed",
      [&observer](
          const std::shared_ptr<atlas::cyber_demo::Status>& message) {
        observer.OnStatus(message);
      });
  if (!reader) {
    return 3;
  }

  const auto deadline =
      std::chrono::steady_clock::now() + std::chrono::seconds(8);
  while (observer.received() < 20 &&
         std::chrono::steady_clock::now() < deadline &&
         apollo::cyber::OK()) {
    std::this_thread::sleep_for(std::chrono::milliseconds(50));
  }

  AINFO << "received=" << observer.received()
        << " gaps=" << observer.gaps()
        << " invalid=" << observer.invalid();
  if (observer.invalid() != 0) {
    return 4;
  }
  if (observer.received() != 20 || observer.gaps() != 0) {
    return 5;
  }
  return 0;
}
