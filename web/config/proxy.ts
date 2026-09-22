export const DEFAULT_QUANTOS_API_TARGET = "http://127.0.0.1:8000";

export function resolveQuantosApiTarget(
  environment: Record<string, string | undefined> = process.env,
): string {
  const candidate = environment.QUANTOS_API_TARGET?.trim() || DEFAULT_QUANTOS_API_TARGET;
  let parsed: URL;
  try { parsed = new URL(candidate); }
  catch { throw new Error("QUANTOS_API_TARGET must be a loopback HTTP origin"); }
  if (
    parsed.protocol !== "http:" || parsed.hostname !== "127.0.0.1" ||
    parsed.username || parsed.password || parsed.pathname !== "/" || parsed.search || parsed.hash
  ) throw new Error("QUANTOS_API_TARGET must be a loopback HTTP origin");
  return parsed.origin;
}
