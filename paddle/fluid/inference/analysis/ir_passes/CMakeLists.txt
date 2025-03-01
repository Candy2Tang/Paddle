cc_library(
  subgraph_util
  SRCS subgraph_util.cc
  DEPS subgraph_detector)

if(WITH_GPU AND TENSORRT_FOUND)
  cc_library(
    tensorrt_subgraph_pass
    SRCS tensorrt_subgraph_pass.cc
    DEPS convert_to_mixed_precision subgraph_util tensorrt_op_teller
         infer_io_utils)

  set(analysis_deps
      ${analysis_deps} subgraph_util tensorrt_subgraph_pass
      CACHE INTERNAL "")

  set(pass_file
      ${PADDLE_BINARY_DIR}/paddle/fluid/inference/api/paddle_inference_pass.h.tmp
  )
  file(APPEND ${pass_file} "USE_PASS(tensorrt_subgraph_pass);\n")
  set(INFER_IR_PASSES
      ${INFER_IR_PASSES} tensorrt_subgraph_pass
      CACHE INTERNAL "")
endif()

if(WITH_LITE)
  cc_library(
    lite_subgraph_pass
    SRCS lite_subgraph_pass.cc
    DEPS ${analysis_deps} subgraph_util lite_op_teller)
  set(analysis_deps
      ${analysis_deps} subgraph_util lite_subgraph_pass
      CACHE INTERNAL "")
  set(pass_file
      ${PADDLE_BINARY_DIR}/paddle/fluid/inference/api/paddle_inference_pass.h.tmp
  )
  file(APPEND ${pass_file} "USE_PASS(lite_subgraph_pass);\n")
  set(INFER_IR_PASSES
      ${INFER_IR_PASSES} lite_subgraph_pass
      CACHE INTERNAL "")
  cc_test(
    lite_subgraph_pass_tester
    SRCS lite_subgraph_pass_tester.cc
    DEPS lite_subgraph_pass gtest glog)
endif()

message("WITH_DLNNE:${WITH_DLNNE}")
if(WITH_DLNNE)
  cc_library(
    dlnne_subgraph_pass
    SRCS dlnne_subgraph_pass.cc
    DEPS ${analysis_deps} subgraph_util)
  set(analysis_deps
      ${analysis_deps} subgraph_util dlnne_subgraph_pass
      CACHE INTERNAL "")

  set(pass_file
      ${PADDLE_BINARY_DIR}/paddle/fluid/inference/api/paddle_inference_pass.h.tmp
  )
  file(APPEND ${pass_file} "USE_PASS(dlnne_subgraph_pass);\n")
  set(INFER_IR_PASSES
      ${INFER_IR_PASSES} dlnne_subgraph_pass
      CACHE INTERNAL "")
endif()
