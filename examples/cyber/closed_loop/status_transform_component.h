#pragma once

#include <memory>

#include "cyber/component/component.h"
#include "cyber/examples/atlas_closed_loop/status.pb.h"

class StatusTransformComponent final
    : public apollo::cyber::Component<atlas::cyber_demo::Status> {
 public:
  bool Init() override;
  bool Proc(const std::shared_ptr<atlas::cyber_demo::Status>& input) override;

 private:
  std::shared_ptr<apollo::cyber::Writer<atlas::cyber_demo::Status>> writer_;
};

CYBER_REGISTER_COMPONENT(StatusTransformComponent)
