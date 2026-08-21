import assert from "node:assert/strict";
import {
  extractVideoPollMediaName,
  extractVideoOperationName,
  isUsableResolvedMediaUrl,
  normalizeVideoOperations,
  normalizeVideoStatusRequest
} from "../browser_extension/providers/veo_operations.mjs";

assert.equal(extractVideoOperationName({ operation: { name: "operations/123" } }), "operations/123");
assert.deepEqual(
  normalizeVideoOperations([
    {
      name: "media/ignored",
      projectId: "project-1",
      operation: {
        name: "operations/123",
        done: false,
        metadata: { newlyAddedByGoogle: true }
      }
    },
    { operation: "operations/456", workflowId: "workflow-2" },
    { name: "operations/789", mediaMetadata: { status: "PENDING" } },
    { operation: { name: "operations/123" } },
    { operation: { done: false } }
  ]),
  [
    { operation: { name: "operations/123" } },
    { operation: { name: "operations/456" } },
    { operation: { name: "operations/789" } }
  ]
);

assert.deepEqual(
  normalizeVideoStatusRequest([
    {
      name: "media/123",
      projectId: "project-1",
      operation: { name: "operations/legacy", metadata: { extra: true } }
    },
    { name: "media/123", projectId: "project-1" },
    { name: "media/456" }
  ], "project-fallback"),
  {
    media: [
      { name: "media/123", projectId: "project-1" },
      { name: "media/456", projectId: "project-fallback" }
    ]
  }
);

assert.deepEqual(
  normalizeVideoStatusRequest([{ operation: { name: "operations/legacy" } }]),
  { operations: [{ operation: { name: "operations/legacy" } }] }
);

assert.equal(
  extractVideoPollMediaName({
    media: [{
      name: "media/generated-video-123",
      mediaMetadata: { mediaStatus: { mediaGenerationStatus: "MEDIA_GENERATION_STATUS_SUCCESSFUL" } }
    }]
  }),
  "media/generated-video-123"
);

assert.equal(extractVideoPollMediaName({ media: [{}] }, "media/submitted-fallback"), "media/submitted-fallback");

const redirectEndpoint = "https://labs.google/fx/api/trpc/media.getMediaUrlRedirect?name=media%2F123";
assert.equal(
  isUsableResolvedMediaUrl(
    "https://flow-content.google/video/media-123?Expires=123&KeyName=test&Signature=signed",
    redirectEndpoint
  ),
  true
);
assert.equal(isUsableResolvedMediaUrl(redirectEndpoint, redirectEndpoint), false);
assert.equal(isUsableResolvedMediaUrl("https://example.com/video/media-123?Expires=1&KeyName=k&Signature=s", redirectEndpoint), false);
assert.equal(isUsableResolvedMediaUrl("https://flow-content.google/video/media-123", redirectEndpoint), false);
assert.equal(isUsableResolvedMediaUrl("javascript:alert(1)", redirectEndpoint), false);

console.log("veo operation normalization tests passed");
