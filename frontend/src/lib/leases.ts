export interface LeaseLike {
  status: string;
  expires_at: string;
}

export function leaseSummary(
  lease: LeaseLike | undefined,
  now = new Date(),
): string | null {
  if (!lease) return null;
  if (lease.status === "active") {
    const remainingMs = new Date(lease.expires_at).getTime() - now.getTime();
    const remainingSeconds = Math.max(0, Math.ceil(remainingMs / 1000));
    return `租约有效 ${remainingSeconds}s / Lease active`;
  }
  if (lease.status === "released") {
    return "租约已释放 / Lease released";
  }
  if (lease.status === "expired") {
    return "租约已超时 / Lease expired";
  }
  return `租约 ${lease.status} / Lease ${lease.status}`;
}
