function nonEmptyString(value) {
  return typeof value === "string" && value.trim() ? value.trim() : "";
}

export function extractVideoOperationName(item) {
  if (!item || typeof item !== "object") return "";

  const operation = item.operation;
  if (typeof operation === "string") return nonEmptyString(operation);
  if (operation && typeof operation === "object") {
    return nonEmptyString(operation.name);
  }
  return nonEmptyString(item.name);
}

export function normalizeVideoOperations(mediaList) {
  const out = [];
  const seen = new Set();
  for (const item of Array.isArray(mediaList) ? mediaList : []) {
    const name = extractVideoOperationName(item);
    if (!name || seen.has(name)) continue;
    seen.add(name);
    // The poll endpoint uses a strict protobuf schema. Never pass through
    // additional fields returned by the submit endpoint.
    out.push({ operation: { name } });
  }
  return out;
}

export function normalizeVideoMediaReferences(mediaList, fallbackProjectId = "") {
  const out = [];
  const seen = new Set();
  for (const item of Array.isArray(mediaList) ? mediaList : []) {
    if (!item || typeof item !== "object") continue;
    const name = nonEmptyString(item.name || item.mediaName);
    const projectId = nonEmptyString(item.projectId || item.project_id || fallbackProjectId);
    if (!name || !projectId) continue;
    const key = `${projectId}\n${name}`;
    if (seen.has(key)) continue;
    seen.add(key);
    out.push({ name, projectId });
  }
  return out;
}

export function normalizeVideoStatusRequest(mediaList, fallbackProjectId = "") {
  const media = normalizeVideoMediaReferences(mediaList, fallbackProjectId);
  if (media.length) return { media };
  return { operations: normalizeVideoOperations(mediaList) };
}

export function extractVideoPollMediaName(resp, fallbackMediaName = "") {
  const media = Array.isArray(resp?.media) ? resp.media : [];
  for (const item of media) {
    if (!item || typeof item !== "object") continue;
    const generationId = item.mediaGenerationId;
    const name = nonEmptyString(
      item.name ||
      item.mediaName ||
      (generationId && typeof generationId === "object" ? generationId.mediaGenerationId : generationId)
    );
    if (name) return name;
  }
  return nonEmptyString(fallbackMediaName);
}

export function isUsableResolvedMediaUrl(candidate, redirectEndpoint = "") {
  const value = nonEmptyString(candidate);
  if (!value) return false;
  try {
    const resolved = new URL(value);
    if (resolved.protocol !== "https:") return false;
    if (resolved.hostname.toLowerCase() !== "flow-content.google") return false;
    if (!resolved.pathname.startsWith("/video/")) return false;
    if (!resolved.searchParams.has("Expires") || !resolved.searchParams.has("KeyName") || !resolved.searchParams.has("Signature")) {
      return false;
    }
    if (redirectEndpoint) {
      const endpoint = new URL(redirectEndpoint);
      if (resolved.origin === endpoint.origin && resolved.pathname === endpoint.pathname) return false;
    }
    return true;
  } catch (_) {
    return false;
  }
}
