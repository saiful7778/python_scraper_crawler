import { Link, router } from "@inertiajs/react";
import { useState } from "react";
import { DashboardShell } from "@/components/dashboard-shell";
import { DeleteDialog } from "@/components/DeleteDialog";
import {
  DashboardShellHeader,
  DashboardShellTitle,
} from "@/components/dashboard-shell/DashboardShellHeader";
import { Button } from "@/components/ui/button";
import {
  Table,
  TableBody,
  TableCell,
  TableHead,
  TableHeader,
  TableRow,
} from "@/components/ui/table";
import { TrashIcon } from "@phosphor-icons/react";

export interface Lead {
  id: string;
  counter: number | null;
  title: string | null;
  categories: string[];
  address: string | null;
  phone: string | null;
  email: string | null;
  website: string | null;
  status: string | null;
}

interface LeadsProps {
  leads: Lead[];
}

export default function Leads({ leads }: LeadsProps) {
  const [openDialog, setOpenDialog] = useState(false);
  const [selectedLead, setSelectedLead] = useState<Lead | null>(null);
  const [isDeleting, setIsDeleting] = useState(false);

  function openDeleteDialog(lead: Lead) {
    setSelectedLead(lead);
    setOpenDialog(true);
  }

  function confirmDelete() {
    if (!selectedLead) {
      return;
    }
    setIsDeleting(true);
    router.delete(`/leads/${selectedLead.id}`, {
      preserveScroll: true,
      onSuccess: () => setOpenDialog(false),
      onFinish: () => setIsDeleting(false),
    });
  }

  return (
    <DashboardShell
      header={
        <DashboardShellHeader>
          <DashboardShellTitle>All Leads</DashboardShellTitle>
        </DashboardShellHeader>
      }
    >
      <Table>
        <TableHeader>
          <TableRow>
            <TableHead>#</TableHead>
            <TableHead>Title</TableHead>
            <TableHead>Category</TableHead>
            <TableHead>Website</TableHead>
            <TableHead className="text-right">Actions</TableHead>
          </TableRow>
        </TableHeader>
        <TableBody>
          {leads.map((lead, index) => (
            <TableRow key={lead.counter ?? index}>
              <TableCell>{lead.counter ?? "—"}</TableCell>
              <TableCell>
                <div>
                  <Link href={`/leads/${lead.id}`} className="hover:underline">
                    {lead.title ?? "—"}
                  </Link>
                  {lead.phone && (
                    <div className="text-muted-foreground">{lead.phone}</div>
                  )}
                  {lead.email && (
                    <div className="text-muted-foreground">{lead.email}</div>
                  )}
                </div>
              </TableCell>
              <TableCell>
                {lead.categories.length > 0 ? lead.categories.join(", ") : "—"}
              </TableCell>
              <TableCell>
                {lead.website ? (
                  <a
                    href={lead.website}
                    target="_blank"
                    className="hover:underline"
                  >
                    {lead.website}
                  </a>
                ) : (
                  "—"
                )}
              </TableCell>
              <TableCell className="text-right">
                <Button
                  variant="destructive"
                  size="icon-sm"
                  onClick={() => openDeleteDialog(lead)}
                >
                  <TrashIcon size={32} />
                  <span className="sr-only">delete lead</span>
                </Button>
              </TableCell>
            </TableRow>
          ))}
          {leads.length === 0 && (
            <TableRow>
              <TableCell
                colSpan={7}
                className="text-center text-muted-foreground"
              >
                No leads found.
              </TableCell>
            </TableRow>
          )}
        </TableBody>
      </Table>
      <DeleteDialog
        openDialog={openDialog}
        setOpenDialog={setOpenDialog}
        onDelete={confirmDelete}
        isDeleteing={isDeleting}
      />
    </DashboardShell>
  );
}
