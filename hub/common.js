function show(id, on) {
  document.getElementById(id).classList.toggle("hidden", !on);
}

function say(id, response, okText) {
  var el = document.getElementById(id);
  if (response.ok) {
    el.textContent = okText;
    return Promise.resolve();
  }
  return response.text().then(function(text) {
    el.textContent = text || "That was not saved.";
  });
}

function requireSession(adminOnly) {
  return fetch("/api/session").then(function(r) { return r.json(); }).then(function(body) {
    if (!body.user) {
      location.href = "/";
      return null;
    }
    if (adminOnly && body.user.role !== "admin") {
      location.href = "/calls";
      return null;
    }
    var who = document.getElementById("who");
    if (who) who.textContent = body.user.username;
    if (body.user.role === "admin") {
      var links = document.querySelectorAll(".admin-only");
      for (var i = 0; i < links.length; i++) links[i].classList.remove("hidden");
    }
    return body.user;
  });
}
