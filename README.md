# Cadastral overlap API

Deploy (Render):

1. Push these files to a GitHub repo.
2. In Render: New + -> Web Service -> connect repo.
3. It autodetects Python. Use build: `pip install -r requirements.txt`
4. Start: `uvicorn main:app --host 0.0.0.0 --port 10000`

Then update `openapi.yaml` -> `servers.url` to your Render URL and paste it into Actions.
