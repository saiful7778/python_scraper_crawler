import {
  AlertDialog,
  AlertDialogCancel,
  AlertDialogContent,
  AlertDialogDescription,
  AlertDialogFooter,
  AlertDialogHeader,
  AlertDialogTitle,
} from "./ui/alert-dialog";
import { Button } from "./ui/button";
import { WarningIcon } from "@phosphor-icons/react";

interface DeleteDialogProps {
  openDialog: boolean;
  setOpenDialog: React.Dispatch<React.SetStateAction<boolean>>;
  onDelete: () => void;
  isDeleteing?: boolean;
}

export function DeleteDialog({
  openDialog,
  setOpenDialog,
  onDelete,
  isDeleteing,
}: DeleteDialogProps) {
  return (
    <AlertDialog open={openDialog} onOpenChange={setOpenDialog}>
      <AlertDialogContent>
        <div className="flex flex-col items-center gap-2">
          <div
            className="flex size-9 shrink-0 items-center justify-center rounded-full border"
            aria-hidden="true"
          >
            <WarningIcon size={32} className="opacity-80" />
          </div>
          <AlertDialogHeader>
            <AlertDialogTitle className="w-full text-center">
              Final confirmation
            </AlertDialogTitle>
            <AlertDialogDescription className="text-center">
              This action cannot be undone. Are you sure to delete. To confirm,
              please enter{" "}
              <span className="font-semibold text-foreground">CONFIRM</span>.
            </AlertDialogDescription>
          </AlertDialogHeader>
        </div>

        <div className="space-y-5">
          <AlertDialogFooter>
            <AlertDialogCancel
              render={
                <Button type="button" variant="outline" className="flex-1" />
              }
            >
              Cancel
            </AlertDialogCancel>
            <Button
              type="button"
              className="flex-1"
              disabled={isDeleteing}
              aria-disabled={isDeleteing}
              onClick={onDelete}
            >
              Delete
            </Button>
          </AlertDialogFooter>
        </div>
      </AlertDialogContent>
    </AlertDialog>
  );
}
