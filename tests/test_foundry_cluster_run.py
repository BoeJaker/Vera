"""Unit tests for swarm_service_cmd — Vera dispatching compute to the Docker Swarm.

Pure builder for `docker service create`; imported via lowercase vera.* (worktree)."""
from vera.foundry.foundry_core import swarm_service_cmd


def test_basic_service():
    c = swarm_service_cmd("myjob", "alpine", replicas=3)
    assert c.startswith("docker service create")
    assert "--name myjob" in c and "--replicas 3" in c and "--detach" in c
    assert c.rstrip().endswith("alpine")


def test_command_is_wrapped_and_quoted():
    c = swarm_service_cmd("j", "alpine", command="echo hi && sleep 5")
    assert "sh -c 'echo hi && sleep 5'" in c


def test_name_is_slugified():
    c = swarm_service_cmd("My Job!!", "alpine")
    assert "--name My-Job" in c      # spaces/`!` -> dashes, trailing dashes stripped
    assert "My Job" not in c


def test_replicas_clamped():
    assert "--replicas 1" in swarm_service_cmd("j", "alpine", replicas=0)
    assert "--replicas 100" in swarm_service_cmd("j", "alpine", replicas=9999)


def test_unsafe_image_rejected():
    assert swarm_service_cmd("j", "") == ""
    assert swarm_service_cmd("j", "alpine; rm -rf /") == ""
    assert swarm_service_cmd("j", "$(evil)") == ""


def test_valid_registry_image_accepted():
    c = swarm_service_cmd("j", "registry.example.com:5000/team/app:v1.2")
    assert "registry.example.com:5000/team/app:v1.2" in c


def test_injection_in_command_stays_quoted():
    c = swarm_service_cmd("j", "alpine", command="x'; docker rm -f $(docker ps -aq)")
    # the whole command is a single shell-quoted argument to sh -c
    assert "sh -c 'x'\"'\"'; docker rm -f $(docker ps -aq)'" in c
