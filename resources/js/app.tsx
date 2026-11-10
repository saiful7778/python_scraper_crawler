import "../css/app.css";
import { createInertiaApp } from "@inertiajs/react";
import type { ComponentType } from "react";
import { createRoot } from "react-dom/client";
import DefaultLayout from "./layouts/DefaultLayout";

createInertiaApp({
  layout: () => DefaultLayout,
  resolve: (name) => {
    const pages = (
      import.meta as ImportMeta & {
        glob<T>(
          pattern: string,
          options: { eager: true; import: "default" }
        ): Record<string, T>;
      }
    ).glob<ComponentType>("./Pages/**/*.tsx", {
      eager: true,
      import: "default",
    });
    return pages[`./Pages/${name}.tsx`];
  },
  setup({ el, App, props }) {
    createRoot(el).render(<App {...props} />);
  },
});
