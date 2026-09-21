(function () {
  const MAX_PEOPLE = 5;
  const SPEAKER_LABELS = ['Speaker A', 'Speaker B', 'Speaker C', 'Speaker D', 'Speaker E'];
  const LANGUAGES = [
    { code: 'en', name: 'English', phrases: ['I see you', "I'm sorry", 'I thank you', 'I forgive you', 'I love you'] },
    { code: 'zu', name: 'Zulu', phrases: ['Ngiyakubona', 'Ngiyaxolisa', 'Ngiyabonga', 'Ngiyakuxolela', 'Ngiyakuthanda'] },
    { code: 'xh', name: 'Xhosa', phrases: ['Ndiyakubona', 'Ndicela uxolo', 'Ndiyabulela', 'Ndiyakuxolela', 'Ndiyakuthanda'] },
    { code: 'af', name: 'Afrikaans', phrases: ['Ek sien jou', 'Ek is jammer', 'Ek dank jou', 'Ek vergewe jou', 'Ek het jou lief'] },
    { code: 'st', name: 'Sesotho', phrases: ['Ke a o bona', 'Ke kopa tshwarelo', 'Ke a o leboha', 'Ke a o tshwarela', 'Ke a o rata'] },
    { code: 'es', name: 'Spanish', phrases: ['Te veo', 'Lo siento', 'Te agradezco', 'Te perdono', 'Te amo'] },
    { code: 'fr', name: 'French', phrases: ['Je te vois', 'Je suis desole', 'Je te remercie', 'Je te pardonne', "Je t'aime"] },
    { code: 'pt', name: 'Portuguese', phrases: ['Eu vejo voce', 'Sinto muito', 'Eu te agradeco', 'Eu te perdoo', 'Eu te amo'] },
    { code: 'ja', name: 'Japanese', phrases: ['あなたを見ています', 'ごめんなさい', 'ありがとう', '許します', '愛しています'] },
  ];

  let arctonePeople = [];
  let arctoneListening = false;
  let currentPerson = null;
  let currentPhraseChecks = [false, false, false, false, false];
  let authTimer = null;
  let pendingRemovePersonId = null;

  function langByCode(code) {
    return LANGUAGES.find((l) => l.code === code) || LANGUAGES[0];
  }

  function optionHtml(selectedCode) {
    return LANGUAGES.map((l) => `<option value="${l.code}"${l.code === selectedCode ? ' selected' : ''}>${l.name}</option>`).join('');
  }

  function nextSpeakerLabel(personId) {
    if (personId && currentPerson && currentPerson.speaker_label) return currentPerson.speaker_label;
    const used = new Set(arctonePeople.map((p) => p.speaker_label));
    return SPEAKER_LABELS.find((label) => !used.has(label)) || SPEAKER_LABELS[arctonePeople.length] || 'Speaker';
  }

  function glowArctone() {
    const led = document.getElementById('arctoneLedPreview');
    if (led) {
      led.classList.remove('glow');
      void led.offsetWidth;
      led.classList.add('glow');
    }
    if (currentPerson && currentPerson.id) {
      const card = document.querySelector(`[data-arctone-person-id="${currentPerson.id}"]`);
      if (card) {
        card.classList.remove('glow');
        void card.offsetWidth;
        card.classList.add('glow');
      }
    }
  }

  function setArctoneStatus(title, detail) {
    const state = document.getElementById('arctoneListenState');
    const body = document.getElementById('arctoneListenDetail');
    if (state) state.textContent = title;
    if (body) body.textContent = detail;
  }

  function renderPhraseList() {
    const list = document.getElementById('arctonePhraseList');
    const hint = document.getElementById('arctonePhraseHint');
    const langSelect = document.getElementById('arctonePersonLanguage');
    if (!list || !langSelect) return;
    const lang = langByCode(langSelect.value);
    list.innerHTML = lang.phrases.map((phrase, idx) => `
      <li class="arctone-phrase-item${currentPhraseChecks[idx] ? ' done' : ''}">
        <span>${phrase}</span>
        <span class="arctone-phrase-check">${currentPhraseChecks[idx] ? 'Heard' : 'Waiting'}</span>
      </li>
    `).join('');
    if (hint) hint.textContent = `Ask the person to speak these five phrases in ${lang.name}.`;
  }

  function renderPeople() {
    const grid = document.getElementById('arctonePeopleGrid');
    const badge = document.getElementById('arctoneCountBadge');
    if (!grid) return;
    if (badge) {
      badge.textContent = `${arctonePeople.length} / ${MAX_PEOPLE} speakers`;
      badge.className = 'badge disconnected';
    }

    const cards = arctonePeople.map((p) => `
      <div class="arctone-person-card" data-arctone-person-id="${p.id}">
        <div class="arctone-person-top">
          <div>
            <div class="arctone-speaker-label">${p.speaker_label}</div>
            <div class="arctone-person-name">${p.name}</div>
          </div>
          <span class="arctone-state-pill${p.authenticated ? ' ready' : ''}">${p.authenticated ? 'Voice sample ready' : 'Needs voice sample'}</span>
        </div>
        <div class="arctone-person-meta">
          Speaks ${p.language_name}<br>
          Translates to ${p.target_language_name}
        </div>
        <div class="arctone-card-actions">
          <button type="button" class="btn-secondary" onclick="arctoneOpenPersonModal(${p.id})">Edit</button>
          <button type="button" class="btn-danger" onclick="arctoneOpenRemoveModal(${p.id})">Remove</button>
        </div>
      </div>
    `);

    cards.push(`
      <button type="button" class="arctone-person-card add" onclick="arctoneOpenPersonModal()" ${arctonePeople.length >= MAX_PEOPLE ? 'disabled' : ''}>
        <span class="arctone-card-plus">+</span>
        <span>${arctonePeople.length >= MAX_PEOPLE ? 'Speaker limit reached' : 'Add person'}</span>
      </button>
    `);
    grid.innerHTML = cards.join('');
  }

  async function loadPeople() {
    try {
      const res = await fetch('/api/arctone/people');
      const data = await res.json().catch(() => ({}));
      arctonePeople = Array.isArray(data.people) ? data.people : [];
      if (typeof window.loadTranslationSpeakers === 'function') window.loadTranslationSpeakers();
      renderPeople();
    } catch (e) {
      setArctoneStatus('Offline', 'Could not load Arctone speaker database.');
    }
  }

  function arctoneOpenPersonModal(personId) {
    if (!personId && arctonePeople.length >= MAX_PEOPLE) {
      if (typeof toast === 'function') toast('Arctone supports up to five speakers', 'error');
      return;
    }
    const sessionTarget = document.getElementById('arctoneSessionTargetLang');
    currentPerson = personId ? arctonePeople.find((p) => Number(p.id) === Number(personId)) : null;
    currentPhraseChecks = currentPerson && currentPerson.authenticated ? [true, true, true, true, true] : [false, false, false, false, false];

    const modal = document.getElementById('arctonePersonModal');
    const title = document.getElementById('arctoneModalTitle');
    const subtitle = document.getElementById('arctoneModalSubtitle');
    const name = document.getElementById('arctonePersonName');
    const speaker = document.getElementById('arctoneSpeakerLabel');
    const lang = document.getElementById('arctonePersonLanguage');
    const target = document.getElementById('arctoneTargetLanguage');

    const spokenCode = currentPerson ? currentPerson.language_code : 'en';
    const targetCode = currentPerson ? currentPerson.target_language_code : ((sessionTarget && sessionTarget.value) || 'en');
    if (title) title.textContent = currentPerson ? 'Edit speaker' : 'Add speaker';
    if (subtitle) subtitle.textContent = `${currentPerson ? currentPerson.speaker_label : nextSpeakerLabel()} voice setup`;
    if (name) name.value = currentPerson ? currentPerson.name : '';
    if (speaker) speaker.value = currentPerson ? currentPerson.speaker_label : nextSpeakerLabel();
    if (lang) lang.innerHTML = optionHtml(spokenCode);
    if (target) target.innerHTML = optionHtml(targetCode);

    renderPhraseList();
    if (modal) modal.classList.add('active');
    if (name) setTimeout(() => name.focus(), 50);
  }

  function arctoneClosePersonModal() {
    if (authTimer) {
      clearInterval(authTimer);
      authTimer = null;
    }
    const modal = document.getElementById('arctonePersonModal');
    if (modal) modal.classList.remove('active');
  }

  function arctoneToggleListening() {
    arctoneListening = !arctoneListening;
    const btn = document.getElementById('arctoneStartListeningBtn');
    if (btn) btn.textContent = arctoneListening ? 'Stop' : 'Start listening';
    setArctoneStatus(
      arctoneListening ? 'Idle' : 'Idle',
      'Live diarization is not connected. Record a voice sample on each person, then pick them on the Translation tab.'
    );
    arctoneListening = false;
    if (btn) btn.textContent = 'Start listening';
  }

  async function arctoneAuthenticateCurrent() {
    const name = (document.getElementById('arctonePersonName')?.value || '').trim();
    if (!name) {
      if (typeof toast === 'function') toast('Enter a person name first', 'error');
      return;
    }
    if (!currentPerson || !currentPerson.id) {
      if (typeof toast === 'function') toast('Save the person first, then record a voice sample', 'error');
      return;
    }
    if (!navigator.mediaDevices || !navigator.mediaDevices.getUserMedia) {
      if (typeof toast === 'function') toast('This browser cannot record a microphone', 'error');
      return;
    }
    setArctoneStatus('Recording', `Recording 4 seconds for ${name}.`);
    let stream;
    try {
      stream = await navigator.mediaDevices.getUserMedia({ audio: true });
      const rec = new MediaRecorder(stream);
      const chunks = [];
      rec.ondataavailable = (ev) => {
        if (ev.data && ev.data.size) chunks.push(ev.data);
      };
      const stopped = new Promise((resolve) => { rec.onstop = resolve; });
      rec.start();
      await new Promise((resolve) => setTimeout(resolve, 4000));
      if (rec.state !== 'inactive') rec.stop();
      await stopped;
      const blob = new Blob(chunks, { type: rec.mimeType || 'audio/webm' });
      if (blob.size < 4096) {
        setArctoneStatus('Sample too short', 'The recording was too small. Try again.');
        if (typeof toast === 'function') toast('Voice sample was too short', 'error');
        return;
      }
      const res = await fetch(`/api/arctone/people/${currentPerson.id}/sample`, {
        method: 'POST',
        headers: { 'Content-Type': blob.type || 'application/octet-stream' },
        body: blob,
      });
      const data = await res.json().catch(() => ({}));
      if (!res.ok) {
        if (typeof toast === 'function') toast(data.detail || 'Could not store voice sample', 'error');
        return;
      }
      currentPerson = data.person || currentPerson;
      currentPhraseChecks = currentPerson.authenticated ? [true, true, true, true, true] : currentPhraseChecks;
      renderPhraseList();
      setArctoneStatus('Voice sample stored', `${name} has a recording on the server.`);
      if (typeof toast === 'function') toast('Voice sample stored');
      await loadPeople();
    } catch (e) {
      setArctoneStatus('Recording failed', 'Allow microphone access and try again.');
      if (typeof toast === 'function') toast('Could not record a voice sample', 'error');
    } finally {
      if (stream) stream.getTracks().forEach((track) => track.stop());
    }
  }

  async function arctoneSaveCurrentPerson() {
    const name = (document.getElementById('arctonePersonName')?.value || '').trim();
    const speakerLabel = (document.getElementById('arctoneSpeakerLabel')?.value || nextSpeakerLabel()).trim();
    const language = langByCode(document.getElementById('arctonePersonLanguage')?.value || 'en');
    const targetLanguage = langByCode(document.getElementById('arctoneTargetLanguage')?.value || 'en');
    if (!name) {
      if (typeof toast === 'function') toast('Enter a person name', 'error');
      return;
    }
    const body = {
      name,
      language_code: language.code,
      language_name: language.name,
      target_language_code: targetLanguage.code,
      target_language_name: targetLanguage.name,
      speaker_label: speakerLabel,
    };
    const id = currentPerson && currentPerson.id;
    try {
      const res = await fetch(id ? `/api/arctone/people/${id}` : '/api/arctone/people', {
        method: id ? 'PATCH' : 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(body),
      });
      const data = await res.json().catch(() => ({}));
      if (!res.ok) {
        if (typeof toast === 'function') toast(data.detail || 'Could not save Arctone person', 'error');
        return;
      }
      await loadPeople();
      arctoneClosePersonModal();
      if (typeof toast === 'function') toast(id ? 'Arctone person updated' : 'Arctone person added');
    } catch (e) {
      if (typeof toast === 'function') toast('Could not save Arctone person', 'error');
    }
  }

  function arctoneOpenRemoveModal(personId) {
    pendingRemovePersonId = personId;
    const person = arctonePeople.find((p) => Number(p.id) === Number(personId));
    const modal = document.getElementById('arctoneRemoveModal');
    const text = document.getElementById('arctoneRemoveText');
    if (text) {
      text.textContent = person
        ? `Remove ${person.name} (${person.speaker_label}) from the Arctone group?`
        : 'Remove this person from the Arctone group?';
    }
    if (modal) modal.classList.add('active');
  }

  function arctoneCloseRemoveModal() {
    pendingRemovePersonId = null;
    const modal = document.getElementById('arctoneRemoveModal');
    if (modal) modal.classList.remove('active');
  }

  async function arctoneConfirmRemovePerson() {
    const personId = pendingRemovePersonId;
    if (!personId) return;
    try {
      const res = await fetch(`/api/arctone/people/${personId}`, { method: 'DELETE' });
      if (!res.ok) {
        const data = await res.json().catch(() => ({}));
        if (typeof toast === 'function') toast(data.detail || 'Could not remove person', 'error');
        return;
      }
      await loadPeople();
      arctoneCloseRemoveModal();
      if (typeof toast === 'function') toast('Arctone person removed');
    } catch (e) {
      if (typeof toast === 'function') toast('Could not remove person', 'error');
    }
  }

  function initArctoneSection() {
    const sessionTarget = document.getElementById('arctoneSessionTargetLang');
    if (!sessionTarget || sessionTarget.dataset.init) return;
    sessionTarget.dataset.init = '1';
    sessionTarget.innerHTML = optionHtml('en');
    const personLang = document.getElementById('arctonePersonLanguage');
    if (personLang) personLang.addEventListener('change', () => {
      currentPhraseChecks = [false, false, false, false, false];
      renderPhraseList();
    });
    const targetLang = document.getElementById('arctoneTargetLanguage');
    if (targetLang) targetLang.addEventListener('change', renderPhraseList);
    const modal = document.getElementById('arctonePersonModal');
    if (modal) {
      modal.addEventListener('click', (ev) => {
        if (ev.target === modal) arctoneClosePersonModal();
      });
    }
    const removeModal = document.getElementById('arctoneRemoveModal');
    if (removeModal) {
      removeModal.addEventListener('click', (ev) => {
        if (ev.target === removeModal) arctoneCloseRemoveModal();
      });
    }
    loadPeople();
  }

  window.initArctoneSection = initArctoneSection;
  window.arctoneOpenPersonModal = arctoneOpenPersonModal;
  window.arctoneClosePersonModal = arctoneClosePersonModal;
  window.arctoneToggleListening = arctoneToggleListening;
  window.arctoneAuthenticateCurrent = arctoneAuthenticateCurrent;
  window.arctoneSaveCurrentPerson = arctoneSaveCurrentPerson;
  window.arctoneOpenRemoveModal = arctoneOpenRemoveModal;
  window.arctoneCloseRemoveModal = arctoneCloseRemoveModal;
  window.arctoneConfirmRemovePerson = arctoneConfirmRemovePerson;

  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', initArctoneSection, { once: true });
  } else {
    initArctoneSection();
  }
})();
