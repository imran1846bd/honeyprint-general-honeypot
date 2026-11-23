/* Show the virtual printer's actual control state beside OctoPrint's controls. */
$(function () {
    function HoneyPrintControlsViewModel(parameters) {
        var printer = parameters[0];
        var status;

        function refresh() {
            if (!status || !printer.isOperational()) return;
            $.getJSON("/api/plugin/honeyprint_audit").done(function (data) {
                var position = data.position;
                var controls = data.controls || {};
                var coordinates = position
                    ? "X " + position.x.toFixed(2) + " · Y " + position.y.toFixed(2) +
                        " · Z " + position.z.toFixed(2) +
                        (typeof position.e === 'number' ? " · E " + position.e.toFixed(2) : "")
                    : "Position: use Home or an axis button to read it";
                status.text(coordinates + " | Motors " + (controls.motors || "unknown") +
                    " | Fan " + controls.fan_percent + "%" +
                    " | Feed " + controls.feed_percent + "%" +
                    " | Flow " + controls.flow_percent + "%");
            });
        }

        this.onAfterBinding = function () {
            status = $('<div id="honeyprint-control-state" class="alert alert-info" role="status" />');
            $('#control-jog-general').after(status);
            status.text("Connect the virtual printer to use its controls. Heat the hotend before extruding.");
            $('#control-jog-xy, #control-jog-z').on('click', 'button', function () {
                setTimeout(function () {
                    OctoPrint.control.sendGcode(['M400', 'M114']).always(function () {
                        setTimeout(refresh, 500);
                    });
                }, 600);
            });
            $('#control-jog-extrusion').on('click', 'button', function () {
                setTimeout(function () {
                    OctoPrint.control.sendGcode(['M400', 'M114']).always(function () {
                        setTimeout(refresh, 500);
                    });
                }, 600);
            });
            $('#control-jog-general, #control-jog-feedrate, #control-jog-flowrate')
                .on('click', 'button', function () { setTimeout(refresh, 800); });
            setInterval(function () {
                if ($('#control-jog-general').is(':visible')) refresh();
            }, 2000);
            refresh();
        };
    }

    OCTOPRINT_VIEWMODELS.push({
        construct: HoneyPrintControlsViewModel,
        dependencies: ['controlViewModel'],
        elements: []
    });
});
