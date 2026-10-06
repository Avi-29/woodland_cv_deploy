/* @odoo-module */

import { Component, useState, onWillStart } from "@odoo/owl";
import { rpc } from "@web/core/network/rpc";
import { registry } from "@web/core/registry";
import { session } from "@web/session";

import { _t } from "@web/core/l10n/translation";

// Shared across every tab/window of this browser profile (same-origin
// localStorage), so activity in one tab resets the countdown for all of
// them, and the deadline itself lives in one place instead of a separate
// in-memory variable per tab.
const STORAGE_KEY = "auto_logout_idle_user_odoo.last_activity";

class TimerSystrayItem extends Component{
    static template= "auto_logout_idle_user_odoo.TimerSystray"
    setup(){
        this.state = useState({
           idle_time: null,
        })

        onWillStart(async () => {
            var self = this
            var now = new Date().getTime();
            rpc('/get_idle_time/timer', {
            }).then((data) => {
                if (data) {
                    self.minutes = data
                    self.idle_timer()
                }
             });
        });

    }
    /**
    reading the shared last-activity timestamp, defaulting to now if no
    tab has recorded one yet
    */
    getLastActivity() {
        var stored = Number(localStorage.getItem(STORAGE_KEY));
        return stored || new Date().getTime();
    }
    /**
    recording activity so every open tab sees the same deadline
    */
    touchActivity() {
        localStorage.setItem(STORAGE_KEY, new Date().getTime());
    }
    /**
    passing values of the countdown to the xml
    */
    idle_timer() {
        var self = this
        self.touchActivity();
        /** Running the count down using setInterval function, always
        re-reading the shared deadline so another tab's activity is
        picked up here too */
        var idle = setInterval(function() {
            var now = new Date().getTime();
            var updatedTimestamp = self.getLastActivity() + self.minutes * 60 * 1000;
            var distance = updatedTimestamp - now;
            var days = Math.floor(distance / (1000 * 60 * 60 * 24));
            var hours = Math.floor((distance % (1000 * 60 * 60 * 24)) / (1000 * 60 * 60));
            var minutes = Math.floor((distance % (1000 * 60 * 60)) / (1000 * 60));
            var seconds = Math.floor((distance % (1000 * 60)) / 1000);
            if (hours && days) {
                self.state.idle_time = days + "d " + hours + "h " + minutes + "m " + seconds + "s ";
            } else if (hours) {
                self.state.idle_time = hours + "h " + minutes + "m " + seconds + "s ";
            } else {
                self.state.idle_time = minutes + "m " + seconds + "s ";
            }
            /** if the countdown is zero the link is redirect to the login page*/
            if (distance < 0) {
                clearInterval(idle);
                self.state.idle_time = "EXPIRED";
                location.replace("/web/session/logout")
            }
        }, 1000);
        /**
        checking if the onmouse-move event is occur
        */
        document.onmousemove = () => {
            self.touchActivity();
        };
         /**
        checking if the onkeypress event is occur
        */
        document.onkeypress = () => {
            self.touchActivity();
        };
        /**
        checking if the onclick event is occur
        */
        document.onclick = () => {
            self.touchActivity();
        };
        /**
        checking if the ontouchstart event is occur
        */
        document.ontouchstart = () => {
            self.touchActivity();
        }
        /**
        checking if the onmousedown event is occur
        */
        document.onmousedown = () => {
            self.touchActivity();
        }
    }
}
export const systrayItem = {
    Component: TimerSystrayItem
};
registry.category("systray").add("auto_logout_idle_user_odoo.TimerSystray",systrayItem, {sequence:25});
