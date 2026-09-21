# Publishing so a hub can find you

A hub can add a backend by typing a command, but the **Add from registry** page
is where people will look. It searches the official MCP registry, shows what a
server declares, and fills the settings form from it — including a typed field
per environment variable, with the description you wrote, instead of a freeform
blob.

Three steps, in order.

## 1. Publish the package

To PyPI or npm, so the hub can launch it with `uvx` or `npx` without you hosting
anything. For PyPI, Trusted Publishing from GitHub Actions avoids a long-lived
token:

```yaml
permissions:
  id-token: write
steps:
  - uses: pypa/gh-action-pypi-publish@release/v1
```

On PyPI, the pending-publisher form's **Workflow name** wants the workflow
*filename* (`ci.yml`), not its display name. A mismatch fails with
`invalid-publisher` and no further explanation. For a project that does not
exist on PyPI yet, create a *pending* publisher — a project that is not there
has no settings tab to add one to.

## 2. Write `server.json`

At the repository root. The registry validates it on publish.

```json
{
  "$schema": "https://static.modelcontextprotocol.io/schemas/2025-09-29/server.schema.json",
  "name": "io.github.you/dictionary",
  "description": "Look words up and propose new ones.",
  "version": "0.2.0",
  "repository": { "url": "https://github.com/you/dictionary", "source": "github" },
  "packages": [
    {
      "registryType": "pypi",
      "identifier": "dictionary-mcp",
      "version": "0.2.0",
      "transport": { "type": "stdio" },
      "environmentVariables": [
        {
          "name": "DICTIONARY_DB",
          "description": "Where the dictionary is kept. Set this to $MCPHUB_STORAGE/words.db under mcphub.",
          "isRequired": false
        }
      ]
    }
  ]
}
```

`environmentVariables` is what turns the hub's generic form into a real one.
Give each a description that says what to put in it — that text is the help
under the box.

The `name` must be in a namespace you can prove you own. `io.github.<you>/...`
is proved by signing in with GitHub.

## 3. Publish to the registry

```bash
mcp-publisher login github
mcp-publisher publish
```

Do it on every release, with `version` matching the package you just pushed.
The hub checks the registry hourly for new versions of the backends it runs, and
offers them on the dashboard; an account can pin one and stay on it.

## Being recognised as built for mcphub

The hub ships a list of servers it has actually launched and probed, with the
result recorded — not a claim in metadata. A badge means a machine started that
server and got the tools it expected. If you want yours on that list, open a
pull request against `scripts/verify_servers.py` with a probe for it: what to
launch, and a tool call whose answer proves it works.
