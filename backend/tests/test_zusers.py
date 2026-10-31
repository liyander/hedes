import httpx
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from conftest import login

from app.core.db import Base, get_db
from app.main import app


def test_admin_user_management(server):
    admin, owner, builder = login(server, "admin"), login(server, "owner"), login(server, "builder")
    assert builder.get("/api/v1/admin/users").status_code == 403
    assert admin.post("/api/v1/admin/users", json={"username": "boss2", "email": "b2@x.io", "role": "owner", "password": "longenough1"}).status_code == 403

    r = admin.post("/api/v1/admin/users", json={"username": "newbie", "name": "New Bie", "email": "newbie@x.io", "role": "user", "password": "first-password-1"})
    assert r.status_code == 200, r.text
    mid = r.json()["id"]
    assert admin.post("/api/v1/admin/users", json={"username": "newbie", "email": "other@x.io", "password": "longenough1"}).status_code == 409
    newbie = httpx.Client(base_url=server, headers={"X-Requested-With": "integra"})
    assert newbie.post("/api/v1/auth/login", json={"username": "newbie", "password": "first-password-1"}).status_code == 200

    assert admin.patch(f"/api/v1/admin/users/{mid}", json={"role": "admin"}).json()["role"] == "admin"
    assert admin.post(f"/api/v1/admin/users/{mid}/password", json={"password": "second-password-2"}).status_code == 200
    assert admin.patch(f"/api/v1/admin/users/{mid}", json={"status": "disabled"}).json()["status"] == "disabled"
    assert newbie.get("/api/v1/auth/me").status_code == 401                      # existing session stops working
    assert httpx.post(f"{server}/api/v1/auth/login", json={"username": "newbie", "password": "second-password-2"}).status_code == 401
    admin.patch(f"/api/v1/admin/users/{mid}", json={"status": "active"})
    assert httpx.post(f"{server}/api/v1/auth/login", json={"username": "newbie", "password": "second-password-2"}).status_code == 200

    users = {u["username"]: u for u in admin.get("/api/v1/admin/users").json()}
    assert admin.patch(f"/api/v1/admin/users/{users['admin']['id']}", json={"status": "disabled"}).status_code == 409   # not yourself
    assert admin.post(f"/api/v1/admin/users/{users['owner']['id']}/password", json={"password": "hijack-attempt"}).status_code == 403
    assert owner.patch(f"/api/v1/admin/users/{users['owner']['id']}", json={"role": "admin"}).status_code == 409        # own role
    # the last active owner cannot be removed, even by another owner
    second = owner.post("/api/v1/admin/users", json={"username": "owner2", "email": "o2@x.io", "role": "owner", "password": "owner-two-pass"}).json()
    o2 = httpx.Client(base_url=server, headers={"X-Requested-With": "integra"})
    o2.post("/api/v1/auth/login", json={"username": "owner2", "password": "owner-two-pass"})
    assert owner.patch(f"/api/v1/admin/users/{second['id']}", json={"status": "disabled"}).status_code == 200
    assert o2.get("/api/v1/auth/me").status_code == 401
    assert owner.patch(f"/api/v1/admin/users/{second['id']}", json={"status": "active"}).status_code == 200
    assert o2.post("/api/v1/auth/login", json={"username": "owner2", "password": "owner-two-pass"}).status_code == 200
    assert o2.patch(f"/api/v1/admin/users/{users['owner']['id']}", json={"role": "admin"}).status_code == 200
    assert owner.get("/api/v1/auth/me").json()["role"] == "admin"
    me2 = o2.get("/api/v1/auth/me").json()
    assert me2["role"] == "owner"
    assert o2.patch(f"/api/v1/admin/users/{users['owner']['id']}", json={"role": "owner"}).status_code == 200  # restore
    assert admin.patch(f"/api/v1/admin/users/{second['id']}", json={"status": "disabled"}).status_code == 403     # admins can't touch owners
    assert any(a["action"] == "user.create" for a in admin.get("/api/v1/audit").json())


def test_setup_is_refused_once_accounts_exist(server):
    r = httpx.post(f"{server}/api/v1/auth/setup", json={"organization": "Evil", "username": "evil", "email": "e@x.io", "password": "takeover-attempt"})
    assert r.status_code == 409
    assert httpx.get(f"{server}/api/v1/auth/setup").json() == {"needs_setup": False}


def test_first_visitor_creates_the_owner_on_an_empty_database(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'fresh.db'}", connect_args={"check_same_thread": False})
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine, expire_on_commit=False)

    def fresh_db():
        db = Session()
        try:
            yield db
        finally:
            db.close()

    app.dependency_overrides[get_db] = fresh_db
    try:
        c = TestClient(app)   # not used as a context manager: the lifespan (seeding the shared test DB) does not run
        c.headers["X-Requested-With"] = "integra"
        assert c.get("/api/v1/auth/setup").json() == {"needs_setup": True}
        bad = c.post("/api/v1/auth/setup", json={"organization": "Acme", "username": "chief", "email": "c@acme.io", "password": "short"})
        assert bad.status_code == 422
        r = c.post("/api/v1/auth/setup", json={"organization": "Acme Security", "username": "chief", "name": "Chief", "email": "chief@acme.io",
                                               "password": "a-strong-password"})
        assert r.status_code == 200, r.text
        assert r.json()["role"] == "owner" and r.json()["organization"]["name"] == "Acme Security"
        assert c.get("/api/v1/auth/me").json()["username"] == "chief"          # signed in straight away
        assert c.get("/api/v1/auth/setup").json() == {"needs_setup": False}
        assert c.post("/api/v1/auth/setup", json={"organization": "X", "username": "second", "email": "s@x.io", "password": "another-password"}).status_code == 409
        assert [u["role"] for u in c.get("/api/v1/admin/users").json()] == ["owner"]
    finally:
        app.dependency_overrides.pop(get_db, None)
        engine.dispose()


def test_front_end_is_served_under_a_versioned_path(server):
    import re
    html = httpx.get(server + "/")
    assert html.headers["cache-control"] == "no-cache"
    src = re.search(r'src="(/static-v/[0-9a-f]+/js/app\.js)"', html.text).group(1)
    js = httpx.get(server + src)
    assert js.status_code == 200 and "immutable" in js.headers["cache-control"] and "export" in js.text
    page = httpx.get(server + src.replace("js/app.js", "js/pages/settings.js"))
    assert page.status_code == 200 and "demo.demo" in page.text
    assert httpx.get(server + src.replace("js/app.js", "../../app/main.py")).status_code == 404
    assert httpx.get(server + src.replace("js/app.js", "..%2F..%2Fmain.py")).status_code == 404
