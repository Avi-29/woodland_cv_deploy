import { KanbanRenderer } from "@web/views/kanban/kanban_renderer";
import { WoodlandDocumentsKanbanRecord } from "./documents_kanban_record";

export class WoodlandDocumentsKanbanRenderer extends KanbanRenderer {
    static components = {
        ...KanbanRenderer.components,
        KanbanRecord: WoodlandDocumentsKanbanRecord,
    };
}
