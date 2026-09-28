#include "cyber/examples/atlas_closed_loop/status_transform_component.h"

bool StatusTransformComponent::Init() {
  writer_ = node_->CreateWriter<atlas::cyber_demo::Status>(
      "/atlas/status/processed");
  return writer_ != nullptr;
}

bool StatusTransformComponent::Proc(
    const std::shared_ptr<atlas::cyber_demo::Status>& input) {
  auto output = std::make_shared<atlas::cyber_demo::Status>(*input);
  output->set_text("processed:" + input->text());
  return writer_->Write(output);
}
