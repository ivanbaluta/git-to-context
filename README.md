# git-to-context (gtc)

`git-to-context` is a command-line tool that flattens any GitHub repository or **local directory** into a single, structured context file. It generates a static HTML page designed for both humans (easy skimming) and LLMs (structured CXML format).

## Features

- **Local & Remote support**: Works with GitHub URLs or local paths (`gtc .`).
- **Git-aware filtering**: Automatically respects `.gitignore` rules and skips binary/oversized files.
- **Dual view modes**:
  - **👤 Human View**: Pretty interface with syntax highlighting and navigation.
  - **🤖 LLM View**: Raw CXML format optimized for copy-pasting into LLMs for deep code analysis.
- **Clean directory tree**: Generates an ASCII tree representing only the files included in the context.
- **Search-friendly**: Single-page layout makes "Ctrl+F" across the entire codebase instant.

## Installation

```bash
git clone https://github.com/ivanbaluta/git-to-context.git
cd git-to-context
pip install -e .
```

## Usage

### Flatten a local project (instant):
```bash
gtc .
```

### Flatten a GitHub repository:
```bash
gtc https://github.com/karpathy/nanoGPT
```

## Credits & License

Based on the original [rendergit](https://github.com/karpathy/rendergit) by Andrej Karpathy.

License: [0BSD](LICENSE)
