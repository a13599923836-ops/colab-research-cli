# Colab Research CLI

`colab-research-cli` is a standalone client for the Colab Research Paperless service.

It can:

- sign in with a Paperless account
- inspect your current role and capabilities
- manage your own MCP tokens
- approve `mcp_access` if your server-side role allows it
- list, show, download, upload, and update documents when your server-side role allows it

The CLI does not bypass server permissions. All authorization is still enforced by the Colab Research server.

## Install

Recommended:

```bash
pipx install git+https://github.com/a13599923836-ops/colab-research-cli.git
```

Alternative:

```bash
python3 -m pip install --user git+https://github.com/a13599923836-ops/colab-research-cli.git
```

## Update

If you installed with `pipx`:

```bash
pipx upgrade colab-research-cli
```

If you installed with `pip`:

```bash
python3 -m pip install --user --upgrade git+https://github.com/a13599923836-ops/colab-research-cli.git
```

## Default Service Endpoint

By default the CLI talks to:

- Paperless: `https://paperless.colab-research.cloud`
- CLI API root: `https://paperless.colab-research.cloud/mcp/cli/v1`

You may override the base URL with `--base-url` during login.

## Login

```bash
colab auth login -u <your-paperless-username> -p '<your-paperless-password>'
```

The CLI stores its local credential at:

- `~/.config/colab-research-cli/config.json`

## Common Commands

```bash
colab me --json
colab token list --json
colab token create --label claude --json
colab access list --json
colab access grant <username>
colab docs list --json
colab docs show <doc_id> --json
colab docs download <doc_id> -o ./document.pdf
colab docs upload ./note.txt --title 标题 --document-type 公司纪要 --storage-path 投研资料库 --tag 待分类 --json
colab docs upload ./batch-a.pdf ./batch-b.pdf --pending --json
colab docs upload ./incoming --recursive --pending --storage-path 投研资料库 --json
colab docs upload ./batch-a.pdf ./batch-b.pdf --metadata-json ./metadata.json --json
colab docs update <doc_id> --title 新标题 --tag 待分类 --json
```

## Batch Upload Flow

- `colab docs upload` accepts one or more files
- directories can be expanded, with `--recursive` for subdirectories
- if you start an interactive upload without metadata, the CLI will prompt:
  - empty-metadata upload
  - classify first with another LLM
- if you choose LLM classification, the CLI prints a prompt you can send to another model
- save the returned JSON and pass it back with `--metadata-json`

Example:

```bash
colab docs upload ./a.pdf ./b.pdf ./c.pdf --metadata-json ./metadata.json --json
```
