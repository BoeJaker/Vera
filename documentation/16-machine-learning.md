# 16 · Machine Learning

The `machine learning/` module is a live neural-network construction, training, and execution sandbox. It is two cooperating files:

- **`ml_workshop.py`** — build and run networks. A "module" is a JSON-serialised compute graph; the workshop assembles, introspects, and forward-passes it.
- **`ml_training.py`** — train them. A full training loop, dataset management, and data collectors wired into the [Data Fabric](./06-data-fabric.md).

Everything is pure-Python + NumPy by default, so it runs anywhere; PyTorch/JAX are used automatically when present.

---

## 1. Modules as compute graphs (Workshop)

Every ML module is a JSON graph of compute nodes:

| Node kind | Examples |
|---|---|
| Layers | Dense, Conv2D, RNN, LSTM, Attention, Embedding, Norm, Dropout |
| Ops | Add, Mul, Concat, Split, Reshape, Transpose, Softmax, … |
| Activations | ReLU, GELU, Swish, Sigmoid, Tanh, SiLU, custom |
| Perceptrons | single / multi-layer (the fundamental building block) |
| Ensembles | MoE, Bagging, Stacking, Boosting metacompositions |
| Exotic | Hopfield, Reservoir/ESN, CapsNet, Kolmogorov-Arnold |

Modules are stored in Redis (hot) and optionally Postgres (cold). They can be built interactively in the panel, **generated from a natural-language description via LLM**, assembled programmatically via caps, called as caps (`ml.run(<module_id>, inputs)`), or composed into a [DAG](./03-dag-engine.md).

### Execution backends

Pure Python + NumPy is always available. The executor auto-detects PyTorch / JAX and chooses the best backend (`HAS_NP`, `HAS_TORCH`). Forward-pass + introspection is the core; training is layered on by `ml_training.py`.

### Workshop capabilities

| Cap | Purpose |
|---|---|
| `ml.catalogue` | The palette of available node types |
| `ml.create` | Create a module from a graph spec |
| `ml.from_template` | Instantiate from a built-in template |
| `ml.list` / `ml.get` / `ml.delete` | Module CRUD |
| `ml.run` | Forward-pass a module on inputs |
| `ml.inspect` | Shapes, parameter counts, graph structure |
| `ml.generate` | **LLM**: build a module from a natural-language description |
| `ml.explain` | **LLM**: explain what a module does |
| `ml.suggest` | **LLM**: suggest next nodes / fixes for the editor |
| `ml.compare` | Compare two modules |

---

## 2. Training engine

`ml_training.py` adds a backprop training loop that needs no PyTorch:

- **Optimisers** — SGD, Adam, AdamW (pure NumPy).
- **Losses** — MSE, MAE, BCE, CrossEntropy, Huber.
- **Backprop** — through the module graph via numerical gradients (finite difference), with exact gradients for standard layers.
- **Loop** — mini-batch training with progress streamed via Redis events; per-epoch loss/accuracy/MAE/RMSE; early stopping; LR scheduling (step / cosine / plateau).
- **Checkpoint** — best weights saved to Redis keyed by `module_id`.

| Cap | Purpose |
|---|---|
| `ml.train` | Start a training run (streams events) |
| `ml.train.from_dataset` | Train directly from a prepared fabric dataset |
| `ml.train.status` | Live status of a run |
| `ml.train.stop` | Cancel a running job |
| `ml.train.history` | Loss/metric curves for a run |
| `ml.train.evaluate` | Evaluate on a held-out test set |
| `ml.train.predict` | Batch inference with trained weights |
| `ml.train.weights_get` | Export trained weights as JSON |
| `ml.train.weights_load` | Load weights into a module |
| `ml.examples.load_all` | Seed the workshop with all worked examples |

---

## 3. Datasets & data collectors

Training data lives in the fabric. The dataset engine provides a typed `DatasetSpec` (train/val/test splits), built-in generators (synthetic classification, regression, time-series, XOR, spiral, moons, circles — all pure NumPy), fabric loaders (OHLCV / any dataset → NumPy arrays), and feature engineering (normalise, standardise, lag features, returns, rolling stats, RSI, MACD, Bollinger bands).

| Cap | Pulls from |
|---|---|
| `ml.data.fetch_synthetic` | Generated synthetic datasets for any example |
| `ml.data.fetch_ohlcv` | Yahoo Finance / Alpha Vantage / Stooq |
| `ml.data.fetch_crypto` | CoinGecko OHLCV for crypto pairs |
| `ml.data.fetch_macro` | FRED macroeconomic series (GDP, CPI, rates) |
| `ml.data.list` | List all ML-ready datasets in the fabric |
| `ml.data.prepare` | Normalise + split a fabric dataset for training |

This is where ML meets [Markets](./15-markets.md): the `mkt.*` datasets that Markets ingests are directly trainable via `ml.data.prepare` and `ml.train.from_dataset`.

---

## 4. UI

- **`ml-workshop-panel`** (`ml_workshop_panel.html`) — the visual graph builder: drag nodes from the catalogue, wire them, run a forward pass, inspect shapes, or ask the LLM to generate a network from a prompt.
- **`ml-training-panel`** (`ml_training_panel.html`) — pick a module + dataset, configure the optimiser/schedule, launch a run, and watch live loss/metric curves stream in.

---

## See also

- [Data Fabric](./06-data-fabric.md) — where datasets and weights checkpoints live
- [Markets](./15-markets.md) — `mkt.*` OHLCV datasets feed `ml.data.prepare`
- [DAG Engine](./03-dag-engine.md) — compose `ml.run` / training steps into workflows
- [Capability Framework](./01-capability-framework.md) — `ml.*` registration & event streaming
