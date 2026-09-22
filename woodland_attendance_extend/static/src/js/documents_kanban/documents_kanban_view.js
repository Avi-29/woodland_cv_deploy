import { registry } from "@web/core/registry";
import { kanbanView } from "@web/views/kanban/kanban_view";
import { WoodlandDocumentsKanbanRenderer } from "./documents_kanban_renderer";

export const woodlandDocumentsKanbanView = {
    ...kanbanView,
    Renderer: WoodlandDocumentsKanbanRenderer,
};

registry.category("views").add("woodland_documents_kanban", woodlandDocumentsKanbanView);
