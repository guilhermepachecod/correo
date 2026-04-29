let currentEmail = null
let mode = "html"
let showImages = true

async function loadEmails(){
  const r = await fetch("/api/emails")
  const data = await r.json()

  document.getElementById("emails").innerHTML =
    data.map(e => `<div onclick="openEmail(${e.id})">${e.subject}</div>`).join("")
}

async function openEmail(id){
  const r = await fetch(`/api/email/${id}`)
  const e = await r.json()
  currentEmail = e

  render()
}

function render(){
  const c = document.getElementById("content")

  let html = (mode === "html" ? currentEmail.body_html : currentEmail.body_text) || ""

  if(!showImages){
    html = html.replace(/<img[^>]*>/g, "")
  }

  c.innerHTML = html
}

function setMode(m){
  mode = m
  render()
}

function toggleImages(){
  showImages = !showImages
  render()
}

loadEmails()