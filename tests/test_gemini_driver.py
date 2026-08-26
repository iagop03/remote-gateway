from remote_gateway.drivers.gemini import GeminiDriver


def test_translate_init_line_captures_gemini_session_id():
    driver = GeminiDriver()
    line = {"type": "init", "session_id": "abc-123", "model": "auto"}
    events, gemini_session_id, result = driver._translate(line, "sess_1", None)
    assert gemini_session_id == "abc-123"
    assert result is None
    assert events[0]["type"] == "session_started"
    assert events[0]["data"]["gemini_session_id"] == "abc-123"


def test_translate_ignores_user_message_echo():
    driver = GeminiDriver()
    line = {"type": "message", "role": "user", "content": "hi"}
    events, _, _ = driver._translate(line, "sess_1", None)
    assert events == []


def test_translate_assistant_delta_emits_assistant_message():
    driver = GeminiDriver()
    line = {"type": "message", "role": "assistant", "content": "Hi", "delta": True}
    events, _, _ = driver._translate(line, "sess_1", "abc-123")
    assert events[0]["type"] == "assistant_message"
    assert events[0]["data"] == {"text": "Hi", "partial": True}


def test_translate_tool_use_emits_tool_started():
    driver = GeminiDriver()
    line = {"type": "tool_use", "tool_id": "t1", "tool_name": "list_directory", "parameters": {"dir_path": "."}}
    events, _, _ = driver._translate(line, "sess_1", None)
    assert events[0]["type"] == "tool_started"
    assert events[0]["data"] == {"id": "t1", "name": "list_directory", "input": {"dir_path": "."}}


def test_translate_tool_result_emits_tool_output():
    driver = GeminiDriver()
    line = {"type": "tool_result", "tool_id": "t1", "status": "success"}
    events, _, _ = driver._translate(line, "sess_1", None)
    assert events[0]["type"] == "tool_output"
    assert events[0]["data"]["tool_use_id"] == "t1"
    assert events[0]["data"]["status"] == "success"


def test_translate_result_line_emits_session_completed():
    driver = GeminiDriver()
    line = {"type": "result", "status": "success", "stats": {"input_tokens": 10, "output_tokens": 4}}
    events, _, result = driver._translate(line, "sess_1", "abc-123")
    assert result is line
    assert events[0]["type"] == "session_completed"
    assert events[0]["data"]["stats"]["output_tokens"] == 4


def test_translate_non_success_result_also_emits_error():
    driver = GeminiDriver()
    line = {"type": "result", "status": "error", "stats": {}}
    events, _, _ = driver._translate(line, "sess_1", None)
    types = [event["type"] for event in events]
    assert types == ["session_completed", "error"]


def test_build_result_message_uses_accumulated_text_not_result_line():
    driver = GeminiDriver()
    result_payload = {"status": "success", "stats": {"input_tokens": 5, "output_tokens": 2}}
    message, status = driver._build_result_message({"model": "gemini:auto"}, result_payload, "Hel" + "lo")
    assert status == "idle"
    assert message["content"] == [{"type": "text", "text": "Hello"}]
    assert message["usage"] == {"input_tokens": 5, "output_tokens": 2}
    assert message["stop_reason"] == "end_turn"


def test_build_command_adds_resume_flag_when_continuing():
    driver = GeminiDriver()
    cmd = driver._build_command({"messages": [{"role": "user", "content": "hi"}], "model": "gemini:auto"}, "abc-123")
    assert cmd[:5] == ["gemini", "-p", "hi", "-o", "stream-json"]
    assert "--skip-trust" in cmd
    assert "--resume" in cmd
    assert cmd[cmd.index("--resume") + 1] == "abc-123"
    assert "--model" not in cmd  # "auto" is the default, no flag needed


def test_build_command_forwards_specific_model():
    driver = GeminiDriver()
    cmd = driver._build_command({"messages": [{"role": "user", "content": "hi"}], "model": "gemini:gemini-3.5-flash"}, None)
    assert "--model" in cmd
    assert cmd[cmd.index("--model") + 1] == "gemini-3.5-flash"
