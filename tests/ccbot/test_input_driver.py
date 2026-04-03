from ccbot.input_driver import RuntimeInputDriver


def test_runtime_input_driver_exposes_registry_helpers() -> None:
    driver = RuntimeInputDriver()

    assert driver.supports_message_routing_mode("claude", "queue")
    assert driver.supports_message_routing_mode("fast-agent", "steer")
    assert driver.supports_interactive_control("codex")
    assert driver.blocked_input_policy("fast-agent") == "fail_closed_on_visible_prompt"
