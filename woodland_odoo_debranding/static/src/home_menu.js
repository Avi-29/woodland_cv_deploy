/** @odoo-module **/

import { patch } from "@web/core/utils/patch";
import { HomeMenu } from "@ica_web_responsive/webclient/home_menu/home_menu";


patch(HomeMenu.prototype, {
    get logoUrl() {
        return "/woodland_odoo_debranding/static/src/img/logo.png";
    },
});