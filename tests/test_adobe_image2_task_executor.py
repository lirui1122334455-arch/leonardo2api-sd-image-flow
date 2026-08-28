import unittest
import base64
import hashlib
from io import BytesIO
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from PIL import Image

from src.api.routes import _normalize_public_image_fields, _normalize_video_task_payload
from src.services.adobe_image2_task_executor import (
    _adobe_browser_upload_image,
    _find_or_open_adobe_page,
    _is_allowed_adobe_poll_url,
    _is_complete_sherlock_token,
    _is_likely_image_url,
    _probe_adobe_account,
    _run_adobe_generation,
    build_adobe_image2_nonce,
    build_adobe_image2_payload,
    decode_adobe_reference_data_url,
    extract_adobe_image_urls,
    extract_adobe_poll_url,
    extract_adobe_upload_blob_id,
    is_adobe_generation_submission_request,
    parse_adobe_account_text,
    parse_adobe_effective_quota,
    parse_adobe_poll_result,
    persist_adobe_account_info,
    resolve_adobe_image2_aspect_ratio,
    resolve_adobe_image2_quality,
    resolve_adobe_image2_reference_sources,
    validate_adobe_image_bytes,
    validate_adobe_reference_image_bytes,
)
from src.services.task_executor_types import NonPenalizedTaskError
from src.services.task_handler_registry import (
    get_create_task_handler,
    get_refresh_quota_handler,
)


class AdobeImage2PayloadTests(unittest.TestCase):
    def test_public_model_routes_to_isolated_adobe_task_type(self):
        task_type, payload = _normalize_video_task_payload(
            {
                "model": "adobe-gpt-image2-high",
                "prompt": "a red paper sculpture",
                "aspect_ratio": "3:2",
            }
        )

        self.assertEqual(task_type, "adobe_image2_workflow")
        self.assertEqual(payload["quality"], "high")
        self.assertEqual(payload["provider_model"], "gpt-image@2")
        self.assertEqual(payload["workflow_kind"], "image")

    def test_public_route_normalizes_image_alias_for_adobe(self):
        body = {
            "model": "adobe-gpt-image2-medium",
            "prompt": "keep the subject and change the lighting",
            "image": "https://cdn.example.com/reference.png",
        }

        _normalize_public_image_fields(body)
        task_type, payload = _normalize_video_task_payload(body)

        self.assertEqual(task_type, "adobe_image2_workflow")
        self.assertEqual(payload["first_image_url"], "https://cdn.example.com/reference.png")
        self.assertEqual(
            resolve_adobe_image2_reference_sources(payload),
            ["https://cdn.example.com/reference.png"],
        )

    def test_existing_gpt_image2_route_is_unchanged(self):
        task_type, payload = _normalize_video_task_payload(
            {"model": "gpt-image2-2k", "prompt": "test"}
        )

        self.assertEqual(task_type, "gpt_workflow")
        self.assertEqual(payload["resolution"], "2k")

    def test_quality_and_ratio_validation(self):
        self.assertEqual(
            resolve_adobe_image2_quality({"model": "adobe-gpt-image2-low"}),
            ("adobe-gpt-image2-low", "low"),
        )
        self.assertEqual(resolve_adobe_image2_aspect_ratio({}), "auto")
        for ratio in (
            "auto",
            "8:1",
            "4:1",
            "21:9",
            "16:9",
            "5:4",
            "4:3",
            "3:2",
            "1:1",
            "4:5",
            "3:4",
            "2:3",
            "9:16",
            "1:4",
            "1:8",
        ):
            self.assertEqual(resolve_adobe_image2_aspect_ratio({"aspect_ratio": ratio}), ratio)
        with self.assertRaises(NonPenalizedTaskError):
            resolve_adobe_image2_aspect_ratio({"aspect_ratio": "7:5"})
        with self.assertRaises(NonPenalizedTaskError):
            resolve_adobe_image2_quality({"quality": "ultra"})

    def test_builds_every_adobe_website_aspect_ratio(self):
        expected_sizes = {
            "auto": (2048, 2048),
            "8:1": (6144, 768),
            "4:1": (4096, 1024),
            "21:9": (3024, 1296),
            "16:9": (2560, 1440),
            "5:4": (2240, 1792),
            "4:3": (2304, 1728),
            "3:2": (2496, 1664),
            "1:1": (2048, 2048),
            "4:5": (1792, 2240),
            "3:4": (1728, 2304),
            "2:3": (1664, 2496),
            "9:16": (1440, 2560),
            "1:4": (1024, 4096),
            "1:8": (768, 6144),
        }

        for quality, detail_level in (("low", 1), ("medium", 3), ("high", 5)):
            for ratio, (width, height) in expected_sizes.items():
                with self.subTest(quality=quality, ratio=ratio):
                    payload = build_adobe_image2_payload(
                        prompt="test",
                        quality=quality,
                        aspect_ratio=ratio,
                        seed=1,
                    )
                    self.assertEqual(payload["size"], {"width": width, "height": height})
                    self.assertEqual(payload["modelSpecificPayload"], {"size": f"{width}x{height}"})
                    self.assertEqual(payload["generationSettings"], {"detailLevel": detail_level})

    def test_builds_reference_gpt_image2_payload(self):
        payload = build_adobe_image2_payload(
            prompt="a red paper sculpture",
            quality="medium",
            aspect_ratio="3:2",
            seed=123456,
        )

        self.assertEqual(payload["modelId"], "gpt-image")
        self.assertEqual(payload["modelVersion"], "2")
        self.assertEqual(payload["seeds"], [123456])
        self.assertEqual(payload["size"], {"width": 2496, "height": 1664})
        self.assertEqual(payload["modelSpecificPayload"], {"size": "2496x1664"})
        self.assertEqual(payload["generationSettings"], {"detailLevel": 3})
        self.assertEqual(payload["outputResolution"], "2K")
        self.assertEqual(payload["referenceBlobs"], [])
        self.assertEqual(payload["generationMetadata"]["module"], "text2image")

    def test_builds_image_to_image_reference_blobs(self):
        payload = build_adobe_image2_payload(
            prompt="change the background",
            quality="medium",
            aspect_ratio="1:1",
            seed=123,
            reference_blob_ids=["blob-a", "blob-b"],
        )

        self.assertEqual(
            payload["referenceBlobs"],
            [
                {"id": "blob-a", "usage": "subject"},
                {"id": "blob-b", "usage": "subject"},
            ],
        )
        self.assertEqual(payload["generationMetadata"]["module"], "image2image")

    def test_collects_deduplicated_single_and_multiple_reference_aliases(self):
        sources = resolve_adobe_image2_reference_sources(
            {
                "image_url": "https://cdn.example.com/a.png",
                "first_image_url": "https://cdn.example.com/a.png",
                "images": [
                    {"url": "https://cdn.example.com/b.png"},
                    "https://cdn.example.com/c.png",
                ],
                "referenceImageUrls": ["https://cdn.example.com/b.png"],
            }
        )

        self.assertEqual(
            sources,
            [
                "https://cdn.example.com/a.png",
                "https://cdn.example.com/b.png",
                "https://cdn.example.com/c.png",
            ],
        )

    def test_rejects_more_than_four_references_and_masks(self):
        with self.assertRaises(NonPenalizedTaskError):
            resolve_adobe_image2_reference_sources(
                {"images": [f"https://cdn.example.com/{index}.png" for index in range(5)]}
            )
        with self.assertRaises(NonPenalizedTaskError):
            resolve_adobe_image2_reference_sources({"mask": "https://cdn.example.com/mask.png"})

    def test_decodes_and_validates_reference_data_url(self):
        output = BytesIO()
        Image.new("RGB", (64, 64), (20, 40, 60)).save(output, format="PNG")
        source = "data:image/png;base64," + base64.b64encode(output.getvalue()).decode("ascii")

        data, mime = decode_adobe_reference_data_url(source)

        self.assertEqual(data, output.getvalue())
        self.assertEqual(mime, "image/png")

    def test_rejects_non_image_data_url_and_oversized_reference(self):
        with self.assertRaises(NonPenalizedTaskError):
            decode_adobe_reference_data_url("data:text/plain;base64,aGVsbG8=")
        with patch(
            "src.services.adobe_image2_task_executor.ADOBE_IMAGE2_MAX_REFERENCE_BYTES",
            10,
        ):
            with self.assertRaises(NonPenalizedTaskError):
                validate_adobe_reference_image_bytes(b"x" * 11)

    def test_extracts_both_adobe_upload_response_shapes(self):
        self.assertEqual(extract_adobe_upload_blob_id({"images": [{"id": "blob-list"}]}), "blob-list")
        self.assertEqual(extract_adobe_upload_blob_id({"id": "blob-top"}), "blob-top")

    def test_auto_ratio_and_quality_levels_match_adobe(self):
        low = build_adobe_image2_payload(prompt="test", quality="low", aspect_ratio="auto", seed=1)
        high = build_adobe_image2_payload(prompt="test", quality="high", aspect_ratio="2:3", seed=2)

        self.assertEqual(low["size"], {"width": 2048, "height": 2048})
        self.assertEqual(low["generationSettings"], {"detailLevel": 1})
        self.assertEqual(high["size"], {"width": 1664, "height": 2496})
        self.assertEqual(high["generationSettings"], {"detailLevel": 5})

    def test_nonce_uses_jwt_user_id_and_first_256_prompt_chars(self):
        token = "header.eyJ1c2VyX2lkIjoidXNlci0xIn0.signature"
        prompt = "x" * 300

        nonce = build_adobe_image2_nonce(token, prompt)

        expected = hashlib.sha256(f"user-1-{'x' * 256}".encode("utf-8")).hexdigest()
        self.assertEqual(nonce, expected)

    def test_handlers_are_registered_without_replacing_gpt(self):
        self.assertTrue(callable(get_create_task_handler("adobe_image2_workflow")))
        self.assertTrue(callable(get_refresh_quota_handler("adobe_firefly_credits")))
        self.assertTrue(callable(get_create_task_handler("gpt_workflow")))


class AdobeImage2AccountTests(unittest.TestCase):
    def test_parses_account_menu_credits(self):
        parsed = parse_adobe_account_text(
            [
                "Rubel Pata",
                "rubelpata7@gmail.com",
                "Premium features",
                "4000/4000 credits left",
                "Next reset: November 26, 2026",
            ]
        )

        self.assertEqual(parsed["platform_account"], "rubelpata7@gmail.com")
        self.assertEqual(parsed["remaining_quota"], 4000)
        self.assertEqual(parsed["provisioned_quota"], 4000)
        self.assertEqual(parsed["plan_title"], "Adobe Firefly Premium")

    def test_parses_bks_effective_quota(self):
        parsed = parse_adobe_effective_quota(
            {
                "effectiveQuotas": [
                    {
                        "resourceType": "firefly_credits",
                        "provisionedQuota": 4000.0,
                        "consumedQuota": 20.0,
                        "nextQuotaRefreshTimestamp": 1795759117,
                    }
                ]
            }
        )

        self.assertEqual(parsed["remaining_quota"], 3980)
        self.assertEqual(parsed["consumed_quota"], 20)
        self.assertEqual(parsed["next_reset_timestamp"], 1795759117)

    def test_extracts_image_assets_without_api_urls(self):
        urls = extract_adobe_image_urls(
            {
                "result": {
                    "imageUrl": "https://cdn.example.com/assets/result.webp?sig=1",
                    "statusUrl": "https://firefly-3p.ff.adobe.io/v2/status/job-1",
                }
            }
        )

        self.assertEqual(urls, ["https://cdn.example.com/assets/result.webp?sig=1"])

    def test_extracts_poll_link_with_header_priority(self):
        self.assertEqual(
            extract_adobe_poll_url(
                {"X-Override-Status-Link": "https://bks-epo1.adobe.io/jobs/header"},
                {"links": {"result": {"href": "https://bks-epo1.adobe.io/jobs/body"}}},
            ),
            "https://bks-epo1.adobe.io/jobs/header",
        )
        self.assertEqual(
            extract_adobe_poll_url({}, {"links": {"result": {"href": "https://bks-epo1.adobe.io/jobs/body"}}}),
            "https://bks-epo1.adobe.io/jobs/body",
        )

    def test_parses_terminal_poll_image(self):
        parsed = parse_adobe_poll_result(
            {
                "status": "SUCCEEDED",
                "outputs": [
                    {"image": {"presignedUrl": "https://cdn.example.com/result.png"}},
                    {"image": {"presignedUrl": "https://cdn.example.com/result-2.png"}},
                ],
            }
        )

        self.assertEqual(parsed["status"], "SUCCEEDED")
        self.assertEqual(parsed["progress"], 100.0)
        self.assertEqual(len(parsed["urls"]), 2)

    def test_poll_token_is_only_sent_to_https_adobe_hosts(self):
        self.assertTrue(_is_allowed_adobe_poll_url("https://bks-epo123.adobe.io/v2/jobs/result/task-1"))
        self.assertFalse(_is_allowed_adobe_poll_url("http://bks-epo123.adobe.io/v2/jobs/result/task-1"))
        self.assertFalse(_is_allowed_adobe_poll_url("https://example.com/steal-token"))

    def test_validates_complete_sherlock_cookie(self):
        token = "eyJzaWQiOiJzIiwiYXJrIjoiYSIsImJmcCI6ImIiLCJmdHIiOiJmIn0="
        self.assertTrue(_is_complete_sherlock_token(token))
        self.assertFalse(_is_complete_sherlock_token("eyJzaWQiOiJzIn0="))


class AdobeImage2GenerationSafetyTests(unittest.TestCase):
    def test_rejects_known_tracking_pixel_url(self):
        self.assertFalse(_is_likely_image_url("https://alb.reddit.com/rp.gif"))

    def test_rejects_one_pixel_gif(self):
        tracking_gif = bytes.fromhex(
            "47494638396101000100800000000000ffffff21f90401000000002c000000000100010000020144003b"
        )

        with self.assertRaises(NonPenalizedTaskError):
            validate_adobe_image_bytes(tracking_gif)

    def test_accepts_generated_image_dimensions(self):
        output = BytesIO()
        Image.new("RGB", (256, 256), (210, 30, 45)).save(output, format="PNG")

        self.assertEqual(validate_adobe_image_bytes(output.getvalue())[:2], (256, 256))

    def test_identifies_adobe_generation_submission_request(self):
        self.assertTrue(
            is_adobe_generation_submission_request(
                url="https://firefly-3p.ff.adobe.io/v2/generate",
                method="POST",
                post_data='{"prompt":"a red paper sculpture","model":"gpt-image@2"}',
                prompt="a red paper sculpture",
            )
        )
        self.assertFalse(
            is_adobe_generation_submission_request(
                url="https://alb.reddit.com/rp.gif",
                method="GET",
                post_data=None,
                prompt="a red paper sculpture",
            )
        )


class AdobeImage2DirectGenerationTests(unittest.IsolatedAsyncioTestCase):
    async def test_uploads_binary_with_only_storage_headers(self):
        page = SimpleNamespace(
            evaluate=AsyncMock(return_value={"status": 201, "data": {"images": [{"id": "blob-1"}]}})
        )

        blob_id = await _adobe_browser_upload_image(
            page,
            token="adobe-token",
            data=b"image-bytes",
            content_type="image/png",
        )

        self.assertEqual(blob_id, "blob-1")
        args = page.evaluate.await_args.args[1]
        self.assertEqual(args["headers"]["Authorization"], "Bearer adobe-token")
        self.assertEqual(args["headers"]["x-api-key"], "clio-playground-web")
        self.assertEqual(args["headers"]["Content-Type"], "image/png")
        self.assertNotIn("x-arp-session-id", args["headers"])
        self.assertNotIn("x-nonce", args["headers"])

    async def test_submits_and_polls_without_ui_controls(self):
        token = "header.eyJ1c2VyX2lkIjoidXNlci0xIn0.signature"
        fetch = AsyncMock(
            side_effect=[
                {
                    "status": 202,
                    "data": {"links": {}},
                    "headers": {
                        "x-override-status-link": "https://bks-epo1.adobe.io/v2/jobs/result/task-1"
                    },
                },
                {
                    "status": 200,
                    "data": {
                        "status": "SUCCEEDED",
                        "outputs": [{"image": {"presignedUrl": "https://cdn.example.com/result.png"}}],
                    },
                    "headers": {},
                },
            ]
        )
        progress = AsyncMock()
        with (
            patch(
                "src.services.adobe_image2_task_executor._capture_adobe_credentials",
                AsyncMock(return_value={"token": token, "sherlock_token": "sherlock"}),
            ),
            patch("src.services.adobe_image2_task_executor._adobe_browser_fetch_json", fetch),
            patch(
                "src.services.adobe_image2_task_executor._persist_remote_images",
                AsyncMock(
                    return_value=[
                        (
                            "https://cdn.example.com/result.png",
                            "/public/adobe-image2-assets/task-direct-0.png",
                        )
                    ]
                ),
            ),
        ):
            result = await _run_adobe_generation(
                object(),
                prompt="a red paper sculpture",
                public_model="adobe-gpt-image2-medium",
                quality="medium",
                aspect_ratio="1:1",
                task_id="task-direct",
                timeout_seconds=120,
                progress_cb=progress,
            )

        self.assertEqual(result["image_url"], "/public/adobe-image2-assets/task-direct-0.png")
        self.assertEqual(result["provider_model"], "gpt-image@2")
        self.assertEqual(fetch.await_count, 2)
        submit = fetch.await_args_list[0].kwargs
        poll = fetch.await_args_list[1].kwargs
        self.assertEqual(submit["method"], "POST")
        self.assertEqual(submit["body"]["modelId"], "gpt-image")
        self.assertEqual(submit["headers"]["x-arp-session-id"], "sherlock")
        self.assertEqual(len(submit["headers"]["x-nonce"]), 64)
        self.assertEqual(poll.get("method", "GET"), "GET")
        self.assertNotIn("x-arp-session-id", poll["headers"])

    async def test_uploads_references_before_image_to_image_submission(self):
        token = "header.eyJ1c2VyX2lkIjoidXNlci0xIn0.signature"
        fetch = AsyncMock(
            side_effect=[
                {
                    "status": 202,
                    "data": {"links": {}},
                    "headers": {
                        "x-override-status-link": "https://bks-epo1.adobe.io/v2/jobs/result/task-i2i"
                    },
                },
                {
                    "status": 200,
                    "data": {
                        "status": "SUCCEEDED",
                        "outputs": [{"image": {"presignedUrl": "https://cdn.example.com/result.png"}}],
                    },
                    "headers": {},
                },
            ]
        )
        load = AsyncMock(
            side_effect=[
                (b"reference-a", "image/png"),
                (b"reference-b", "image/jpeg"),
            ]
        )
        upload = AsyncMock(side_effect=["blob-a", "blob-b"])
        progress = AsyncMock()
        with (
            patch(
                "src.services.adobe_image2_task_executor._capture_adobe_credentials",
                AsyncMock(return_value={"token": token, "sherlock_token": "sherlock"}),
            ),
            patch("src.services.adobe_image2_task_executor._load_adobe_reference_image", load),
            patch("src.services.adobe_image2_task_executor._adobe_browser_upload_image", upload),
            patch("src.services.adobe_image2_task_executor._adobe_browser_fetch_json", fetch),
            patch(
                "src.services.adobe_image2_task_executor._persist_remote_images",
                AsyncMock(
                    return_value=[
                        (
                            "https://cdn.example.com/result.png",
                            "/public/adobe-image2-assets/task-i2i-0.png",
                        )
                    ]
                ),
            ),
        ):
            result = await _run_adobe_generation(
                object(),
                prompt="preserve the subject and change the background",
                public_model="adobe-gpt-image2-medium",
                quality="medium",
                aspect_ratio="1:1",
                task_id="task-i2i",
                timeout_seconds=120,
                progress_cb=progress,
                reference_sources=["https://cdn.example.com/a.png", "https://cdn.example.com/b.jpg"],
            )

        self.assertEqual(load.await_count, 2)
        self.assertEqual(upload.await_count, 2)
        self.assertEqual(fetch.await_count, 2)
        submit = fetch.await_args_list[0].kwargs
        self.assertEqual(submit["body"]["generationMetadata"]["module"], "image2image")
        self.assertEqual(
            submit["body"]["referenceBlobs"],
            [
                {"id": "blob-a", "usage": "subject"},
                {"id": "blob-b", "usage": "subject"},
            ],
        )
        self.assertEqual(result["generation_mode"], "image2image")
        self.assertEqual(result["reference_count"], 2)


class AdobeImage2AccountFallbackTests(unittest.IsolatedAsyncioTestCase):
    @staticmethod
    def _fake_page(url):
        page = SimpleNamespace(
            url=url,
            is_closed=lambda: False,
            goto=AsyncMock(),
            bring_to_front=AsyncMock(),
        )
        return page

    async def test_opens_studio_when_only_other_adobe_pages_exist(self):
        legacy_page = self._fake_page("https://firefly.adobe.com/generate/image")
        studio_page = self._fake_page("about:blank")
        context = SimpleNamespace(
            pages=[legacy_page],
            new_page=AsyncMock(return_value=studio_page),
        )
        pw_ctx = SimpleNamespace(context=context, page=None)

        result = await _find_or_open_adobe_page(
            pw_ctx,
            "https://firefly.adobe.com/studio",
        )

        self.assertIs(result, studio_page)
        context.new_page.assert_awaited_once()
        studio_page.goto.assert_awaited_once_with(
            "https://firefly.adobe.com/studio",
            wait_until="domcontentloaded",
            timeout=90_000,
        )
        legacy_page.goto.assert_not_awaited()

    async def test_reuses_existing_studio_page(self):
        studio_page = self._fake_page("https://firefly.adobe.com/studio?currentProjectId=123")
        context = SimpleNamespace(
            pages=[studio_page],
            new_page=AsyncMock(),
        )
        pw_ctx = SimpleNamespace(context=context, page=None)

        result = await _find_or_open_adobe_page(
            pw_ctx,
            "https://firefly.adobe.com/studio",
        )

        self.assertIs(result, studio_page)
        context.new_page.assert_not_awaited()

    async def test_account_menu_ui_error_falls_back_to_quota_api(self):
        quota = {
            "remaining_quota": 4000,
            "provisioned_quota": 4000,
            "consumed_quota": 0,
            "next_reset_timestamp": 1795759117,
        }
        with (
            patch(
                "src.services.adobe_image2_task_executor._probe_account_menu",
                AsyncMock(side_effect=RuntimeError("pointer intercepted")),
            ),
            patch(
                "src.services.adobe_image2_task_executor._capture_quota_from_reload",
                AsyncMock(return_value=quota),
            ),
        ):
            result = await _probe_adobe_account(object())

        self.assertEqual(result["remaining_quota"], 4000)
        self.assertEqual(result["plan_title"], "Adobe Firefly")

    async def test_missing_account_menu_falls_back_to_quota_api(self):
        page = SimpleNamespace(locator=lambda _selector: SimpleNamespace(count=AsyncMock(return_value=0)))
        quota = {
            "remaining_quota": 4000,
            "provisioned_quota": 4000,
            "consumed_quota": 0,
            "next_reset_timestamp": 1795759117,
        }
        with patch(
            "src.services.adobe_image2_task_executor._capture_quota_from_reload",
            AsyncMock(return_value=quota),
        ):
            result = await _probe_adobe_account(page)

        self.assertEqual(result["remaining_quota"], 4000)
        self.assertEqual(result["plan_title"], "Adobe Firefly")

    async def test_confirmed_login_error_is_not_hidden_by_fallback(self):
        login_error = NonPenalizedTaskError(
            "login missing",
            status_code=401,
            retryable=False,
        )
        with patch(
            "src.services.adobe_image2_task_executor._probe_account_menu",
            AsyncMock(side_effect=login_error),
        ):
            with self.assertRaises(NonPenalizedTaskError) as captured:
                await _probe_adobe_account(object())

        self.assertEqual(captured.exception.status_code, 401)

    async def test_shared_window_keeps_platform_account_label_unchanged(self):
        window = SimpleNamespace(
            id=58,
            space_pk=4,
            window_key="window-39",
            bound_task_type_count=0,
        )
        db = SimpleNamespace(
            update_task_type_window=AsyncMock(),
            get_task_type_window_context=AsyncMock(return_value={"window_pk": 58}),
            get_window=AsyncMock(return_value=window),
            list_windows=AsyncMock(
                return_value=[SimpleNamespace(id=58, bound_task_type_count=2)]
            ),
            update_window_platform_binding=AsyncMock(),
        )

        await persist_adobe_account_info(
            db,
            69,
            {
                "remaining_quota": 4000,
                "provisioned_quota": 4000,
                "platform_account": "adobe@example.com",
                "plan_title": "Adobe Firefly",
            },
        )

        db.update_task_type_window.assert_awaited_once()
        self.assertEqual(db.update_task_type_window.await_args.kwargs["daily_quota"], 4000)
        db.update_window_platform_binding.assert_not_awaited()

    async def test_dedicated_adobe_window_updates_platform_account_label(self):
        window = SimpleNamespace(
            id=59,
            space_pk=4,
            window_key="window-40",
            bound_task_type_count=0,
        )
        db = SimpleNamespace(
            update_task_type_window=AsyncMock(),
            get_task_type_window_context=AsyncMock(return_value={"window_pk": 59}),
            get_window=AsyncMock(return_value=window),
            list_windows=AsyncMock(
                return_value=[SimpleNamespace(id=59, bound_task_type_count=1)]
            ),
            update_window_platform_binding=AsyncMock(),
        )

        await persist_adobe_account_info(
            db,
            68,
            {
                "remaining_quota": 3980,
                "provisioned_quota": 4000,
                "platform_account": "adobe@example.com",
                "plan_title": "Adobe Firefly",
            },
        )

        self.assertEqual(db.update_task_type_window.await_args.kwargs["daily_quota"], 4000)
        db.update_window_platform_binding.assert_awaited_once_with(
            space_pk=4,
            window_key="window-40",
            platform_account_id=None,
            platform_account="adobe@example.com",
            platform_url="https://firefly.adobe.com/studio",
        )


if __name__ == "__main__":
    unittest.main()
