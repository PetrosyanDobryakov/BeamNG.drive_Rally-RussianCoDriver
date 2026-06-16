-- This Source Code Form is subject to the terms of the bCDDL, v. 1.1.
-- If a copy of the bCDDL was not distributed with this
-- file, You can obtain one at http://beamng.com/bCDDL-1.1.txt

local dequeue = require('dequeue')
local rallyUtil = require('/lua/ge/extensions/gameplay/rally/util')

-- === ДОБАВЛЕНО: создаём UDP-сокет ===
local socket = require('socket')
local udp = socket.udp()
udp:setpeername('127.0.0.1', 12347)   -- порт, который слушает Python
-- ===================================

local C = {}
local logTag = ''

function C:init(rallyManager)
  self.rallyManager = rallyManager
  self.pacenoteMetadataOfflineStructured = nil
  self.pacenoteMetadataOnlineStructuredAndFreeform = nil

  self.queue = dequeue.new()
  self.currAudioObj = nil

  self:_loadPacenoteMetadata()
end

function C:_loadPacenoteMetadata()
end

function C:resetQueue()
  self.queue = dequeue.new()
  if self.currAudioObj then
    self:_stopAudio()
  end
  self.currAudioObj = nil
end

function C:_stopAudio()
  Engine.Audio.intercomStopPacenote()
end

function C:handleDamage()
  if self.currAudioObj then
    if not self.currAudioObj.damage then
      if self.currAudioObj.sourceId then
        self:_stopAudio()
        self.currAudioObj = nil
        self.queue = dequeue.new()
      end
    end
  end
end

function C:enqueueDamage()
end

function C:enqueuePauseSecs(secs, addToFront)
  addToFront = addToFront or false
  local pauseAudioObj = {
    audioType = 'pause',
    audioLen = secs,
    time = nil,
    timeout = nil,
  }
  if addToFront then
    self.queue:push_left(pauseAudioObj)
  else
    self.queue:push_right(pauseAudioObj)
  end
end

local audioObjs = nil
function C:enqueuePacenoteAudio(pacenote, addToFront)
  profilerPushEvent("AudioManager - enqueuePacenoteAudio")

  -- === ДОБАВЛЕНО: отправляем структурированные компоненты ===
  if pacenote.notes and pacenote.notes.english and pacenote.notes.english.note then
    local structured = pacenote.notes.english.note.structured
    if structured then
      udp:send('PHRASE:' .. table.concat(structured, '|'))
    end
  end
  -- =========================================================

  audioObjs = pacenote:audioObjs()
  if not audioObjs then
    log('E', logTag, "enqueuePacenoteAudio: no audio objects found")
    profilerPopEvent("AudioManager - enqueuePacenoteAudio")
    return
  end

  for _, audioObj in ipairs(audioObjs) do
    self:_enqueueAudioObj(audioObj, addToFront)
  end

  profilerPopEvent("AudioManager - enqueuePacenoteAudio")
end

function C:enqueueSystemPacenote(pacenote, addToFront, audioLen)
  if pacenote then
    -- === ДОБАВЛЕНО: отправка системного сообщения ===
    if pacenote.text then
      udp:send('SYSTEM:' .. pacenote.text)
    end
    -- ==============================================

    log('D', logTag, string.format("RallyMode: playing system pacenote: '%s'", pacenote.text))

    -- Check if system audio file exists
    if not pacenote.audioFname or pacenote.audioFname == '' or not FS:fileExists(pacenote.audioFname) then
      log('E', logTag, string.format("enqueueSystemPacenote: audio file not found: %s", tostring(pacenote.audioFname)))
      guihooks.message(string.format("Can't find audio file for system pacenote '%s'.", pacenote.name), 5)
      return
    end

    -- Use provided audioLen or default to 1.0 seconds for system pacenotes
    local systemAudioLen = audioLen or 1.0

    -- Create audioObj for system pacenote
    local audioObj = {
      audioType = 'pacenote',
      pacenoteFname = pacenote.audioFname,
      audioLen = systemAudioLen,
      breathSuffixTime = 0.1,
      time = nil,
      timeout = nil,
      pacenote = { name = pacenote.name }  -- minimal pacenote reference for system notes
    }

    self:_enqueueAudioObj(audioObj, addToFront)
  else
    log('E', logTag, string.format("enqueueSystemPacenote: couldnt find system pacenote with name '%s'", pacenote.name))
  end
end

function C:_enqueueAudioObj(audioObj, addToFront)
  addToFront = addToFront or false
  if not audioObj then
    log('E', logTag, "_enqueueAudioObj: no audioObj provided")
    return
  end
  if addToFront then
    self.queue:push_left(audioObj)
  else
    self.queue:push_right(audioObj)
  end
end

function C:isPlaying()
  if self.currAudioObj and self.currAudioObj.timeout then
    return rallyUtil.getTime() < self.currAudioObj.timeout
  else
    return false
  end
end

local queueInfo = {}
function C:getQueueInfo()
  queueInfo.queueSize = self.queue:length()
  queueInfo.paused = not self:isPlaying()
  return queueInfo
end

local playbackObj = {filename=nil}

function C:playNextInQueue()
  if not self:isPlaying() then
    self.currAudioObj = self.queue:pop_left()
    if self.currAudioObj then
      if self.currAudioObj.audioType == 'pacenote' then
        self.currAudioObj.time = rallyUtil.getTime()
        playbackObj.filename = self.currAudioObj.pacenoteFname
        if self.currAudioObj.audioLen then
          self.currAudioObj.timeout = self.currAudioObj.time + self.currAudioObj.audioLen + self.currAudioObj.breathSuffixTime
        end
        Engine.Audio.intercomPlayPacenote(playbackObj)   -- ОРИГИНАЛЬНЫЙ ВЫЗОВ
      elseif self.currAudioObj.audioType == 'pause' then
        log('D', logTag, string.format("playing a pause: secs=%0.2f", self.currAudioObj.audioLen))
        self.currAudioObj.time = rallyUtil.getTime()
        self.currAudioObj.timeout = self.currAudioObj.time + self.currAudioObj.audioLen
      else
        log('E', logTag, string.format('unknown audioType: %s', self.currAudioObj.audioType))
      end
    end
  end
end

function C:onUpdate(dtReal, dtSim, dtRaw)
  profilerPushEvent("AudioManager - onUpdate")
  self:playNextInQueue()
  profilerPopEvent("AudioManager - onUpdate")
end

return function(...)
  local o = {}
  setmetatable(o, C)
  C.__index = C
  o:init(...)
  return o
end