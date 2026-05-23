from pathlib import Path

from ccbot.attachment_batching import (
    MAX_ATTACHMENTS_PER_BATCH,
    MAX_BATCH_DOWNLOADED_BYTES,
    AttachmentBatcher,
    AttachmentBatchFailure,
    AttachmentBatchItem,
    AttachmentTextFragment,
    CapturedBindingTarget,
    IngressBatchKey,
)


def _target(window_id: str = "@7", *, owner: int = 1) -> CapturedBindingTarget:
    return CapturedBindingTarget(
        requesting_user_id=1,
        binding_owner_user_id=owner,
        binding_kind="direct" if owner == 1 else "shared",
        surface_key="t:42",
        chat_id=100,
        message_thread_id=42,
        window_id=window_id,
        binding_generation=(3, "nonce"),
    )


def _key(media_group_id: str | None = None, *, window_id: str = "@7") -> IngressBatchKey:
    target = _target(window_id)
    return IngressBatchKey.from_target(target, media_group_id=media_group_id)


def test_media_group_waits_for_idle_without_total_count():
    batcher = AttachmentBatcher()
    key = _key("album-1")
    batcher.add_attachment(
        key,
        _target(),
        AttachmentBatchItem(
            kind="document",
            path=Path("/tmp/a.txt"),
            display_name="a.txt",
            downloaded_size=1,
            order=1,
        ),
        now=10.0,
    )
    batcher.add_attachment(
        key,
        _target(),
        AttachmentBatchItem(
            kind="document",
            path=Path("/tmp/b.txt"),
            display_name="b.txt",
            downloaded_size=1,
            order=2,
        ),
        now=10.5,
    )

    assert batcher.flush_due_at(key) == 11.5
    assert not batcher.is_due(key, now=11.49)
    assert batcher.is_due(key, now=11.5)


def test_open_batch_lookup_matches_same_control_surface_target():
    batcher = AttachmentBatcher()
    target = _target()
    key = IngressBatchKey.from_target(target, media_group_id="album-1")
    batcher.add_attachment(
        key,
        target,
        AttachmentBatchItem(
            kind="document",
            path=Path("/tmp/a.txt"),
            display_name="a.txt",
            downloaded_size=1,
            order=1,
        ),
        now=10.0,
    )

    assert batcher.has_open_batch_for_target(_target())
    assert not batcher.has_open_batch_for_target(_target(window_id="@8"))


def test_media_group_caption_does_not_flush_before_idle_window():
    batcher = AttachmentBatcher()
    key = _key("album-with-caption")
    target = _target()
    batcher.add_attachment(
        key,
        target,
        AttachmentBatchItem(
            kind="document",
            path=Path("/tmp/a.txt"),
            display_name="a.txt",
            downloaded_size=1,
            order=1,
        ),
        now=10.0,
    )
    batcher.add_text(key, target, AttachmentTextFragment("install these", 2), now=10.0)

    assert batcher.flush_due_at(key) == 11.0
    assert not batcher.is_due(key, now=10.0)
    assert not batcher.is_due(key, now=10.99)
    assert batcher.is_due(key, now=11.0)


def test_orphan_attachment_hold_extends_on_each_new_file():
    batcher = AttachmentBatcher()
    key = _key()
    batcher.add_attachment(
        key,
        _target(),
        AttachmentBatchItem("document", Path("/tmp/a.txt"), "a.txt", 1, 1),
        now=0.0,
    )
    assert batcher.flush_due_at(key) == 15.0
    batcher.add_attachment(
        key,
        _target(),
        AttachmentBatchItem("document", Path("/tmp/b.txt"), "b.txt", 1, 2),
        now=12.0,
    )
    assert not batcher.is_due(key, now=12.0)
    assert batcher.flush_due_at(key) == 27.0


def test_orphan_attachment_flushes_early_when_instruction_arrives():
    batcher = AttachmentBatcher()
    key = _key()
    batcher.add_attachment(
        key,
        _target(),
        AttachmentBatchItem("document", Path("/tmp/a.txt"), "a.txt", 1, 1),
        now=0.0,
    )
    assert not batcher.is_sufficiently_complete(key, now=1.0)

    batcher.add_text(key, _target(), AttachmentTextFragment("install this", 2), now=12.0)

    assert batcher.is_sufficiently_complete(key, now=12.0)
    assert batcher.is_due(key, now=12.0)


def test_sufficiently_complete_definition():
    batcher = AttachmentBatcher()
    key = _key()
    batcher.add_attachment(
        key,
        _target(),
        AttachmentBatchItem("document", Path("/tmp/a.txt"), "a.txt", 1, 1),
        now=0.0,
    )
    assert not batcher.is_sufficiently_complete(key, now=14.9)
    assert batcher.is_sufficiently_complete(key, now=15.0)

    with_text = _key(window_id="@8")
    target = _target("@8")
    batcher.add_attachment(
        with_text,
        target,
        AttachmentBatchItem("document", Path("/tmp/b.txt"), "b.txt", 1, 1),
        now=0.0,
    )
    batcher.add_text(with_text, target, AttachmentTextFragment("use this", 2), now=1.0)
    assert batcher.is_sufficiently_complete(with_text, now=1.0)


def test_text_only_fast_path_not_orphan_held():
    batcher = AttachmentBatcher()
    key = _key()
    batcher.add_text(key, _target(), AttachmentTextFragment("hello", 1), now=0.0)
    assert batcher.flush_due_at(key) == 0.75
    assert not batcher.is_due(key, now=0.74)
    assert batcher.is_due(key, now=0.75)


def test_format_runtime_input_preserves_arrival_order():
    batcher = AttachmentBatcher()
    key = _key()
    target = _target()
    batcher.add_text(key, target, AttachmentTextFragment("first", 1), now=0.0)
    batcher.add_attachment(
        key,
        target,
        AttachmentBatchItem("document", Path("/tmp/a.txt"), "a.txt", 2, 2),
        now=0.1,
    )
    batcher.add_text(key, target, AttachmentTextFragment("caption", 3), now=0.2)
    batcher.add_attachment(
        key,
        target,
        AttachmentBatchItem("image", Path("/tmp/b.jpg"), "b.jpg", 3, 4),
        now=0.3,
    )

    payload = batcher.format_runtime_input(key)

    assert payload.startswith("first\n\ncaption\n\nAttachments:")
    assert "1. document: /tmp/a.txt" in payload
    assert "2. image: /tmp/b.jpg" in payload


def test_format_runtime_input_includes_material_download_failures():
    batcher = AttachmentBatcher()
    key = _key()
    target = _target()
    batcher.add_text(key, target, AttachmentTextFragment("use these", 1), now=0.0)
    batcher.add_attachment(
        key,
        target,
        AttachmentBatchItem("document", Path("/tmp/a.txt"), "a.txt", 1, 2),
        now=0.1,
    )
    batcher.add_failure(
        key,
        target,
        AttachmentBatchFailure("document", "b.txt", "download failed", 3),
        now=0.2,
    )

    payload = batcher.format_runtime_input(key)

    assert "Attachment download failures:" in payload
    assert "- document: b.txt — download failed" in payload


def test_all_downloads_failed_not_usable():
    batcher = AttachmentBatcher()
    key = _key()
    batcher.add_failure(
        key,
        _target(),
        AttachmentBatchFailure("document", "b.txt", "download failed", 1),
        now=0.0,
    )
    assert not batcher.has_usable_content(key)
    assert not batcher.is_due(key, now=14.9)
    assert batcher.is_due(key, now=15.0)


def test_failed_attachment_with_instruction_is_not_runtime_sendable():
    batcher = AttachmentBatcher()
    key = _key()
    target = _target()
    batcher.add_failure(
        key,
        target,
        AttachmentBatchFailure("document", "README.md", "getFile failed", 1),
        now=0.0,
    )
    batcher.add_text(
        key,
        target,
        AttachmentTextFragment("use this if it downloaded", 2),
        now=0.1,
    )

    assert batcher.is_due(key, now=0.1)
    assert not batcher.has_sendable_runtime_input(key)
    payload = batcher.format_runtime_input(key)
    assert "use this if it downloaded" in payload
    assert "- document: README.md — getFile failed" in payload


def test_progress_display_names_never_use_local_paths():
    batcher = AttachmentBatcher()
    key = _key()
    batcher.add_attachment(
        key,
        _target(),
        AttachmentBatchItem(
            "document",
            Path("/data/iqdoctor/.ccbot/documents/secret_README.md"),
            "README.md",
            1,
            1,
        ),
        now=0.0,
    )

    progress = batcher.progress_text(key)

    assert "README.md" in progress
    assert "/data/iqdoctor/.ccbot" not in progress
    assert "secret_README" not in progress


def test_batch_key_separates_surface_routing_grouping_and_target():
    base = _target()
    keys = {
        IngressBatchKey.from_target(base, media_group_id=None),
        IngressBatchKey.from_target(base, media_group_id="album"),
        IngressBatchKey.from_target(_target("@8"), media_group_id=None),
        IngressBatchKey.from_target(_target(owner=2), media_group_id=None),
        IngressBatchKey(
            requesting_user_id=2,
            surface_key="t:42",
            chat_id=100,
            message_thread_id=42,
            media_group_id=None,
            binding_owner_user_id=1,
            binding_kind="direct",
            window_id="@7",
        ),
    }
    assert len(keys) == 5


def test_captured_binding_target_records_shared_binding_provenance():
    target = _target(owner=2)
    assert target.requesting_user_id == 1
    assert target.binding_owner_user_id == 2
    assert target.binding_kind == "shared"
    assert target.binding_generation == (3, "nonce")


def test_limit_flushes_on_eleventh_attachment():
    batcher = AttachmentBatcher()
    key = _key()
    target = _target()
    for idx in range(MAX_ATTACHMENTS_PER_BATCH):
        batcher.add_attachment(
            key,
            target,
            AttachmentBatchItem(
                "document",
                Path(f"/tmp/{idx}.txt"),
                f"{idx}.txt",
                1,
                idx,
            ),
            now=float(idx),
        )
    assert batcher.is_full(key)
    assert batcher.should_start_new_batch_for_attachment(key, downloaded_size=1)


def test_aggregate_byte_cap_uses_downloaded_bytes():
    batcher = AttachmentBatcher()
    key = _key()
    target = _target()
    batcher.add_attachment(
        key,
        target,
        AttachmentBatchItem(
            "document",
            Path("/tmp/big.bin"),
            "big.bin",
            MAX_BATCH_DOWNLOADED_BYTES,
            1,
        ),
        now=0.0,
    )
    assert batcher.should_start_new_batch_for_attachment(key, downloaded_size=1)
