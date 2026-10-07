Put the ONNX embedding model here (mounted read-only into the database container at /opt/oracle/models).

Phase 0 uses Oracle's prebuilt all-MiniLM-L12-v2 model:
1. Download all_MiniLM_L12_v2_augmented.zip (link in docs/SETUP.md)
2. Unzip so this folder contains `all_MiniLM_L12_v2.onnx`
3. Run `podman compose run --rm api emaild load-model`
