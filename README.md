# Dufflepud

A media server for the Coracle nostr client.

# Installation Guide

First, get the repository and install dependencies. You'll need to have [poetry](https://python-poetry.org/) installed.

```
git clone https://github.com/coracle-social/dufflepud.git
cd dufflepud
poetry install
```

Next, fill out the environment file by running `cp env.template env.local` and adding values for the linkpreview api, database url, and s3 bucket information. If you're running this on a PaaS, you'll want to use their environment settings since env.local is not committed to version control.

Finally, enter `poetry run ./start` to start the server.
