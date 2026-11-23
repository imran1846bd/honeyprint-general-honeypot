/*
 * progressbar binding
 *
 * Options:
 *   - barClass: additional class list for the bar, supports observables
 *   - percentage: percentage to display (value 0-100), supports observables
 *   - text: text to display, supports observables
 *   - active: whether to mark active/striped, true or false, supports observables
 */

ko.bindingHandlers.progressbar = {
    init: (element, valueAccessor, allBindings, viewModel, bindingContext) => {
        const val = ko.utils.unwrapObservable(valueAccessor());

        const bar = $("<div class='bar'></div>");
        const textBack = $("<span class='progress-text-back'></span>");
        const textFront = $("<span class='progress-text-front'></span>");

        $(element)
            .empty()
            .addClass("progress progress-text-centered")
            .append(bar)
            .append(textBack)
            .append(textFront);
    },
    update: (element, valueAccessor, allBindings, viewModel, bindingContext) => {
        const val = ko.utils.unwrapObservable(valueAccessor());

        const defaultOptions = {
            barClass: "",
            text: "",
            percentage: 0,
            active: false
        };

        const options = {};
        _.each(defaultOptions, (value, key) => {
            options[key] = ko.utils.unwrapObservable(val[key]) || value;
        });

        const percentage =
            _.isNumber(options.percentage) &&
            0 <= options.percentage &&
            options.percentage <= 100
                ? options.percentage
                : 0;

        const bar = $(".bar", element);
        const textBack = $(".progress-text-back", element);
        const textFront = $(".progress-text-front", element);

        bar.css("width", `${percentage}%`);
        textBack.text(options.text).css("clip-path", `inset(0 0 0 ${percentage}%)`);
        textFront
            .text(options.text)
            .css("clip-path", `inset(0 calc(100% - ${percentage}%) 0 0)`);

        if (options.active) {
            $(element).addClass("progress-striped active");
        } else {
            $(element).removeClass("progress-striped active");
        }

        const currentClasses = bar.attr("class").replace(/(^|\s+)bar(\s+|$)/, "");
        if (currentClasses !== options.barClass) {
            bar.removeClass(currentClasses).addClass(options.barClass);
        }
    }
};
