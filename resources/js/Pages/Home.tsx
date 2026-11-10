import { DashboardShell } from "@/components/dashboard-shell";
import {
  DashboardShellHeader,
  DashboardShellTitle,
} from "@/components/dashboard-shell/DashboardShellHeader";
import { Button } from "@/components/ui/button";

export default function Home() {
  return (
    <DashboardShell
      header={
        <DashboardShellHeader>
          <DashboardShellTitle>Welcome back</DashboardShellTitle>
        </DashboardShellHeader>
      }
    >
      <Button>Click</Button>
    </DashboardShell>
  );
}
