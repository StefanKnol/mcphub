# Storage

A plugin that keeps something gets a directory of its own at
`<data dir>/apps/<slug>`, inside the hub's data volume, so it is backed up with
everything else. The path is shown on the backend's **App** settings page.

It is opt-in, and the default is no:

```python
class DictionaryPlugin(PluginDefaults):
    def uses_storage(self, instance) -> bool:
        return True
```

A server the hub merely proxies gets none. It keeps its data wherever it
already keeps it, and a path the hub names but cannot hand across a container
boundary is a setting that looks like a feature — on the settings page of every
server anyone ever wraps. If you are writing a plugin that needs somewhere to
write, say so and you have one.

## If the hub launches your server

A server the hub starts is its own subprocess, so it is simply handed the path:

```
MCPHUB_STORAGE=/data/apps/dictionary
```

Almost no server asks for its data directory under that name — yours probably
wants `DB_PATH` or `STATE_DIR` or a `--data` flag. So `$MCPHUB_STORAGE` in any
environment value configured for the backend is replaced with the real path:

```
DB_PATH=$MCPHUB_STORAGE/words.db
```

Nothing else would expand that. A launched server is handed its environment
directly, with no shell in between.

Read the variable if you have it, and fall back to your own default if you do
not — your server should still run outside a hub.

```python
import os
from pathlib import Path

DATA = Path(os.environ.get("MCPHUB_STORAGE") or Path.home() / ".dictionary")
DATA.mkdir(parents=True, exist_ok=True)
```

## If your app runs somewhere else

A backend reached over a URL is in another container or on another machine, and
the hub cannot hand a directory across that boundary. Where a plugin has asked
for one, the hub creates it and tells you where it is; mounting it is yours to
do:

```
-v /data/apps/dictionary:/data
```

The hub could instead serve a storage API for apps to call back into. That would
cross the boundary — but only for an app written against it. A directory works
for anything that can be told where to put its files.

## Who shares it

One directory per backend. Every account using it and every version of it share
that one directory, and that is the point: a dictionary two people are editing
while one of them is trying a newer release of the server is the case storage
exists for. A directory per account would give them two dictionaries.

It is not free, and the hub cannot make it free:

- **Two versions share a schema migration.** If the newer one migrates on
  start-up, the older one is now running against migrated data. If you version
  a schema, check it and refuse to start rather than half-reading it.
- **Two processes share the file.** SQLite in WAL mode handles concurrent
  readers and a writer; a plain file you rewrite wholesale does not. Write
  atomically — temp file, then rename — or take a lock.

Where versions keep something they genuinely cannot share, such as a derived
index whose format changed, a backend can carry `storage_per_version` in its
config and each version gets `apps/<slug>@<version>`. That separates the files.
It does not make a shared dataset safe.

## What the hub does with it

- Creates it when the backend is saved, when its App page is opened, and when
  it starts — for a plugin that asked for one.
- Moves it when the backend is renamed.
- **Leaves it alone when the backend is deleted.** Unmounting is reversible and
  a dropped database is not.
