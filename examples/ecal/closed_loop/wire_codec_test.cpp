#include "wire_codec.h"

#include <cassert>
#include <string>

int main() {
  using atlas::ecal_demo::Decode;
  using atlas::ecal_demo::Encode;
  using atlas::ecal_demo::Frame;

  const Frame original{42, 123456789, "processed:raw"};
  const std::string wire = Encode(original);
  const auto decoded = Decode(wire);

  assert(decoded.has_value());
  assert(decoded->sequence == original.sequence);
  assert(decoded->timestamp_us == original.timestamp_us);
  assert(decoded->payload == original.payload);
  assert(!Decode("missing-separators").has_value());
  assert(!Decode("abc|123|raw").has_value());
  assert(!Decode("1|xyz|raw").has_value());
}
