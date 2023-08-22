# lo0py

## Name
Loopback App: Python version

## Installation

### Poetry
```bash
curl -sSL https://install.python-poetry.org | python3 -
echo 'export PATH="/home/wyre/.local/bin:$PATH"' >> ~/.bashrc
poetry completions bash >> ~/.bash_completion
```

### Clearing out preexisting lo.app Caddy data

```bash
ids=$(curl -s localhost:2019/id/lo0app_handler | jq -r '.routes | map(select(has("@id"))) | map(.["@id"]) | .[] | @text');
for app in ${ids}; do
  curl -X DELETE "http://localhost:2019/id/$app";
done
```

## Usage
```bash
ulimit -Sn 1024000
sudo setcap 'cap_net_bind_service=+ep' $(readlink -f $(poetry run which python))
poetry run python -m lo0py
```
