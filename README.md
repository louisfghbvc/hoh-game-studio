<div align="center">
  <h1>⚔️ Harness of Harness (HoH) - Game Studio</h1>
  <p><strong>Multi-Day Autonomous Software Development with Continual Improvement</strong></p>
  <p><em>Full reproduction of arXiv:2609.01481 framework applied to 2D Metroidvania Game Development in Godot 4.</em></p>

  <p>
    <a href="https://arxiv.org/abs/2609.01481"><img src="https://img.shields.io/badge/Paper-2609.01481-8F3B45?style=for-the-badge&logo=arxiv&logoColor=white" alt="Paper"></a>
    <a href="https://godotengine.org/"><img src="https://img.shields.io/badge/Engine-Godot%204.7-478CBF?style=for-the-badge&logo=godotengine&logoColor=white" alt="Godot 4"></a>
    <a href="https://github.com/louisfghbvc/hoh-game-studio"><img src="https://img.shields.io/badge/GitHub-181717?style=for-the-badge&logo=github&logoColor=white" alt="GitHub"></a>
  </p>
</div>

<hr>

## 📖 Overview

**Harness-of-Harness (HoH)** is an AI research framework introduced in arXiv:2609.01481 designed to enable LLM-based coding agents to perform long-horizon, autonomous software development with continual improvement.

Rather than attempting to build a complex software system in a single prompt, HoH wraps coding agents in an outer **planning–coding–testing loop**. Development is scannable, versioned, and constrained into small, testable increments.

This repository provides a 100% faithful reproduction of the HoH infrastructure, applied to creating **VoidKnight**, a 2D Hollow Knight-style Metroidvania game in Godot 4.

---

## 🏛️ Architecture & How It Works

Each iteration executes a structured loop coordinating three distinct roles:

```
       ┌────────────────────────┐
       │     Product PRD.md     │
       └───────────┬────────────┘
                   │
                   ▼
       ┌────────────────────────┐
 ┌────►│  1. Project Planner   │ (gameloop-planner-bot: Reads PRD & Issue Ledger)
 │     └───────────┬────────────┘
 │                 │
 │                 ▼
 │     ┌────────────────────────┐
 │     │      2. Developer      │ (gameloop-developer-bot: Implements code & preserves passing features)
 │     └───────────┬────────────┘
 │                 │
 │                 ▼
 │     ┌────────────────────────┐
 │     │  3. QA & Quality Gate  │ (gameloop-tester-bot: Runs headless Godot & host tests)
 │     └───────────┬────────────┘
 │                 │
 └─────────────────┘
```

### Roles & Push Policy
* 📋 **Project Planner** (`gameloop-planner-bot`): Reads the global `PRD.md` and `issue_ledger.json`, writing a prioritized `development_doc.md`. (Read-only)
* 💻 **Developer** (`gameloop-developer-bot`): Implements planned features in the shared `game/` workspace while preserving verified functionality.
* 🧪 **QA Tester** (`gameloop-tester-bot`): Executes host-owned tests (`godot --headless`), generating immutable Receipts and verdicts (`pass` / `fail`).
* 🔀 **Two-Branch Git Flow**:
  - Pass Host Gate $\rightarrow$ Push candidate to `gameloop` development branch.
  - Pass QA Evaluation $\rightarrow$ Merge `gameloop` into `main` production branch.

---

## 📁 Repository Structure

```
hoh-game-studio/
├── .gameloop/                  # HoH State Infrastructure
│   ├── vcs.json                # Two-branch VCS binding & candidate tree SHA256 hashes
│   ├── issue_ledger.json       # Persistent bug & regression ledger
│   └── receipts/               # Immutable JSON loop receipts (Planner / Dev / Tester)
├── hoh_engine/                 # Python HoH Framework Engine
│   ├── vcs_manager.py          # Tree SHA256 hashing & gameloop -> main git flow
│   ├── issue_ledger.py         # Bug tracker (open, closed, regressed)
│   ├── quality_gate.py         # Host-owned test suite (Godot headless compilation)
│   └── receipt_manager.py      # Immutable receipt generator
├── game/                       # Godot 4 2D Metroidvania Workspace (VoidKnight)
│   ├── project.godot
│   ├── scenes/
│   └── scripts/
├── PRD.md                      # Product Requirements Specification & 75-Loop Roadmap
├── hoh_runner.py               # Main Orchestrator CLI
└── README.md                   # Project documentation
```

---

## 🚀 Quick Start

### 1. Prerequisites
- [Godot 4.x](https://godotengine.org/)
- Python 3.10+
- Git & Git LFS

### 2. Run an Automated HoH Loop
To execute a HoH iteration loop:
```bash
./hoh_runner.py --loop 3 --summary "Implement Pogo Jump and Down Attack Mechanics"
```

### 3. Launch & Play in Godot 4
Open the game in Godot Editor or launch directly via terminal:
```bash
godot --path game
```

---

## 📜 Citation

```bibtex
@article{yan2026harness,
  title={Harness of Harness: Multi-Day Autonomous Software Development with Continual Improvement},
  author={Yan, Haoyang and Su, Min-Le and Zhang, Hangfan and Li, Zhanhao and Zhang, Chen and Zhang, Shao and Chen, Yang and Bai, Lei and Hu, Shuyue},
  journal={arXiv preprint},
  eprint={2609.01481},
  archivePrefix={arXiv},
  year={2026}
}
```

---

## 📄 License
Released under the [MIT License](LICENSE).
