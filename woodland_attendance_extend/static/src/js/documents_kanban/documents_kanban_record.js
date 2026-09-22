import { CANCEL_GLOBAL_CLICK, KanbanRecord } from "@web/views/kanban/kanban_record";
import { useFileViewer } from "@web/core/file_viewer/file_viewer_hook";
import { FileModel } from "@web/core/file_viewer/file_model";

export class WoodlandDocumentsKanbanRecord extends KanbanRecord {
    setup() {
        super.setup();
        this.fileViewer = useFileViewer();
    }

    /**
     * @override
     * Open the file preview (image/pdf/video/text viewer) instead of the
     * plain ir.attachment form when clicking the thumbnail.
     */
    onGlobalClick(ev) {
        if (ev.target.closest(CANCEL_GLOBAL_CLICK)) {
            return;
        } else if (ev.target.closest(".o_kanban_previewer")) {
            const { id, name, mimetype } = this.props.record.data;
            const file = Object.assign(new FileModel(), { id, name, mimetype, type: "binary" });
            this.fileViewer.open(file);
            return;
        }
        return super.onGlobalClick(...arguments);
    }
}
