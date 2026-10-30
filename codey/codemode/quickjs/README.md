# QuickJS/WASM Runtime

Vendored from the MIT-licensed `quickjs-wasi@3.6.2` npm distribution.
Only the engine wrapper, its internal modules and WASM binary are included.
Extensions, filesystem access and module loaders are not enabled.

Upstream tarball SHA-512 integrity:
`sha512-FCqGtGOrMgzUiIrMNMA2YnsOxCNwo31dzqXvclXUC6xeT35NJLKXQJsvbeCTjvoFAwZgEAPg8U6+KAPDGXn8Mg==`.

The Python recovery contract hashes every shipped runtime asset. Updating any
asset requires rerunning CodeMode isolation, quotas, cancellation and durable
recovery tests. Old control-flow recovery rejects changed contracts.
