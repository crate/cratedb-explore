# Marks schemas/ as a package so the generated *_pb2 modules import under a
# single, stable module path (prevents protobuf descriptor-pool double
# registration). The .avsc and .proto source files live here too.
