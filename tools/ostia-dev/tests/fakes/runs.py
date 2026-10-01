"""A core.Run built from real profiles and suites, for the k8s tests."""

from pathlib import Path

from ostia_dev import config
from ostia_dev.remote import core, profiles, suites
from ostia_dev.remote.profiles import parse_duration
from ostia_dev.remote.tarball import Tarball

RUN_ID = "k8s-l4-20261002-141501-a1b2c3"


def make_run(
    tmp_path: Path,
    profile: str = "l4",
    provider: str | None = "gke",
    env: str | None = None,
    *,
    cfg: config.Config | None = None,
    suite: str | None = None,
    command: list[str] | None = None,
    env_vars: dict[str, str] | None = None,
    run_id: str = RUN_ID,
) -> core.Run:
    cfg = cfg or config.load(tmp_path / "none.toml")
    p = profiles.resolve(profile, provider, cfg)
    env = env or ("cuda-12" if p.is_gpu else "default")
    plan = suites.build_plan(cfg, p, env, suite=suite, command=command, run_id=run_id)
    tb = Tarball(
        path=tmp_path / "upload.tar",
        size=1234,
        tree_hash="9ab3" + "0" * 60,
        files=["a"],
        file_count=1,
    )
    spec = core.RunSpec(
        backend="k8s", profile=profile, envs=[env], results=tmp_path / "res", provider=provider
    )
    return core.Run(
        spec=spec,
        env=env,
        run_id=run_id,
        profile=p,
        plan=plan,
        tarball=tb,
        results_dir=tmp_path / "res" / run_id,
        repo=tmp_path,
        git_sha="3f2a9c1+dirty",
        env_vars=env_vars or {},
        windows={k: parse_duration(v) for k, v in cfg.windows.items()},
        image=cfg.image,
    )
