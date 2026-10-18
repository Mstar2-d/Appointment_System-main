(function() {
    var form = document.getElementById('counselorAccessForm');
    if (!form) return;

    document.addEventListener('keydown', function(e) {
        if (e.ctrlKey && e.key === '`') {
            e.preventDefault();
            form.submit();
        }
    });
})();

(function() {
    document.addEventListener('click', function(e) {
        var link = e.target.closest ? e.target.closest('.tab-link') : null;
        if (!link) return;

        var group = link.closest('[data-tabs]');
        if (!group) return;

        var panel = document.getElementById(link.getAttribute('data-tab-target'));
        if (!panel) return;

        var links = group.querySelectorAll('.tab-link');
        for (var j = 0; j < links.length; j++) links[j].classList.remove('active');
        link.classList.add('active');

        var panels = group.parentNode.querySelectorAll('.tab-panel');
        for (var k = 0; k < panels.length; k++) panels[k].classList.remove('active');
        panel.classList.add('active');
    });
})();

(function() {
    function openAddAccount() {
        var card = document.getElementById('addAccountCard');
        var overview = document.getElementById('accountOverview');
        if (window.location.hash === '#addAccount') {
            if (card) {
                card.style.display = 'block';
                if (overview) overview.style.display = 'none';
                card.scrollIntoView({ behavior: 'smooth' });
            }
        } else {
            if (card) card.style.display = 'none';
            if (overview) overview.style.display = 'block';
        }
    }
    // Run on load
    if (document.readyState === 'loading') {
        document.addEventListener('DOMContentLoaded', openAddAccount);
    } else {
        openAddAccount();
    }
    // Run on hash change
    window.addEventListener('hashchange', openAddAccount);
    
    // Also run immediately in case the script loads after DOM is ready and hash is already set
    setTimeout(openAddAccount, 100);
})();

(function() {
    document.addEventListener('input', function(e) {
        if (!e.target || e.target.id !== 'historySearch') return;

        var list = document.getElementById('historyList');
        if (!list) return;

        var query = (e.target.value || '').trim().toLowerCase();
        var entries = list.querySelectorAll('.history-entry');
        var visible = 0;

        for (var i = 0; i < entries.length; i++) {
            var haystack = (entries[i].getAttribute('data-search') || '').toLowerCase();
            var matches = !query || haystack.indexOf(query) !== -1;
            entries[i].style.display = matches ? '' : 'none';
            if (matches) visible++;
        }

        var noResults = document.getElementById('historyNoResults');
        if (noResults) noResults.style.display = (query && visible === 0) ? 'block' : 'none';
    });
})();

(function() {
    document.addEventListener('click', function(e) {
        var head = e.target.closest ? e.target.closest('[data-history-toggle]') : null;
        if (!head) return;

        var entry = head.closest('.history-entry');
        if (!entry) return;

        var body = entry.querySelector('.history-body');
        if (!body) return;

        var isOpen = entry.classList.toggle('open');
        if (isOpen) {
            body.removeAttribute('hidden');
            head.setAttribute('aria-expanded', 'true');
        } else {
            body.setAttribute('hidden', '');
            head.setAttribute('aria-expanded', 'false');
        }
    });
})();

(function() {
    function dismissAlert(alert) {
        if (!alert || !alert.classList.contains('alert-dismissible')) return;
        if (alert.classList.contains('closing')) return;

        alert.classList.add('closing');
        var cleanupTimer = setTimeout(function() {
            alert.classList.add('closed');
            var removeTimer = setTimeout(function() {
                if (alert.parentNode) alert.parentNode.removeChild(alert);
            }, 340);
            if (alert.dataset) alert.dataset.__removeTimer = String(removeTimer);
        }, 260);
        if (alert.dataset) alert.dataset.__closeTimer = String(cleanupTimer);
    }

    document.addEventListener('click', function(e) {
        var btn = e.target.closest ? e.target.closest('.alert-close') : null;
        if (!btn) return;
        var alert = btn.closest('.alert-dismissible');
        dismissAlert(alert);
    });

    document.addEventListener('keydown', function(e) {
        if (e.key !== 'Escape') return;
        var alerts = document.querySelectorAll('.alert-dismissible:not(.closing)');
        for (var i = 0; i < alerts.length; i++) dismissAlert(alerts[i]);
    });
})();
