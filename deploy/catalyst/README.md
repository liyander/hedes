# Deploying H.A.D.E.S to Zoho Catalyst AppSail

AppSail's managed Python runtime runs the app; its dependencies are bundled, built for Linux.

1. **Project folder** (outside this repository, because `app-config.json` holds secrets):

   ```bash
   mkdir hades-catalyst && cd hades-catalyst
   catalyst project:use <project id> --org <org id>
   catalyst appsail:add --name hades --stack python_3_12 --source "$PWD/app" --build build --command "python3 -u start.py"
   ```

2. **Bundle** the app and its Linux dependencies into `app/build`:

   ```bash
   B=app/build && mkdir -p $B/backend/data
   cp -r <repo>/backend/app $B/backend/ && cp -r <repo>/shared <repo>/examples $B/
   cp <repo>/deploy/catalyst/start.py $B/
   grep -v '^pytest' <repo>/backend/requirements.txt > app/requirements-deploy.txt
   python -m pip install -r app/requirements-deploy.txt --target $B/deps --implementation cp --python-version 3.12 \
     --only-binary=:all: --platform manylinux_2_17_x86_64 --platform manylinux2014_x86_64 --platform manylinux_2_28_x86_64
   ```

3. **Configure** `app/app-config.json` from [`app-config.example.json`](app-config.example.json):
   - `DATABASE_URL`: PostgreSQL (for example Neon).
   - `JWT_SECRET` and `SECRET_ENCRYPTION_KEY`: the same values as every other instance using that database. The
     encryption key is what decrypts the vault.
   - `PUBLIC_BASE_URL`: the AppSail URL. Links, OAuth redirect URIs and alerts use it.

4. **Deploy:** `catalyst deploy --only appsail`.

`start.py` listens on `X_ZOHO_CATALYST_LISTEN_PORT`. AppSail restarts an app that isn't listening within about 15
seconds, so startup reads the whole database schema in one query. If startup fails, set `HEDES_DIAGNOSE=1` and the
error is served on the app's URL (with secrets masked) until you remove it.
