#pragma once

#include <charconv>
#include <cstdint>
#include <optional>
#include <string>
#include <string_view>

namespace atlas::ecal_demo {

struct Frame {
  std::uint64_t sequence{};
  std::int64_t timestamp_us{};
  std::string payload;
};

inline std::string Encode(const Frame& frame) {
  std::string wire;
  wire.reserve(48 + frame.payload.size());
  wire += std::to_string(frame.sequence);
  wire.push_back('|');
  wire += std::to_string(frame.timestamp_us);
  wire.push_back('|');
  wire += frame.payload;
  return wire;
}

inline std::optional<Frame> Decode(std::string_view wire) {
  const std::size_t first = wire.find('|');
  if (first == std::string_view::npos) {
    return std::nullopt;
  }
  const std::size_t second = wire.find('|', first + 1);
  if (second == std::string_view::npos) {
    return std::nullopt;
  }

  Frame frame;
  const auto sequence_text = wire.substr(0, first);
  const auto timestamp_text = wire.substr(first + 1, second - first - 1);

  const char* seq_begin = sequence_text.data();
  const char* seq_end = seq_begin + sequence_text.size();
  const auto seq_result =
      std::from_chars(seq_begin, seq_end, frame.sequence);
  if (seq_result.ec != std::errc{} || seq_result.ptr != seq_end) {
    return std::nullopt;
  }

  const char* ts_begin = timestamp_text.data();
  const char* ts_end = ts_begin + timestamp_text.size();
  const auto ts_result =
      std::from_chars(ts_begin, ts_end, frame.timestamp_us);
  if (ts_result.ec != std::errc{} || ts_result.ptr != ts_end) {
    return std::nullopt;
  }

  // The incoming string_view may point into eCAL callback memory. Copy the
  // payload so Frame owns everything after Decode returns.
  frame.payload.assign(wire.substr(second + 1));
  return frame;
}

}  // namespace atlas::ecal_demo
