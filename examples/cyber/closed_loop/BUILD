load("//tools/proto:proto.bzl", "proto_library")
load(
    "//tools:apollo_package.bzl",
    "apollo_cc_binary",
    "apollo_component",
    "apollo_package",
)

package(default_visibility = ["//visibility:public"])

proto_library(
    name = "status_proto",
    srcs = ["status.proto"],
)

apollo_component(
    name = "libstatus_transform_component.so",
    srcs = ["status_transform_component.cc"],
    hdrs = ["status_transform_component.h"],
    deps = [
        "//cyber",
        ":status_cc_proto",
    ],
)

apollo_cc_binary(
    name = "status_source",
    srcs = ["status_source.cc"],
    deps = [
        "//cyber",
        ":status_cc_proto",
    ],
)

apollo_cc_binary(
    name = "status_observer",
    srcs = ["status_observer.cc"],
    deps = [
        "//cyber",
        ":status_cc_proto",
    ],
)

filegroup(
    name = "conf",
    srcs = ["status_transform.dag"],
)

apollo_package()
