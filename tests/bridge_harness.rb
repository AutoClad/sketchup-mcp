# frozen_string_literal: true

# Drives SU_MCP::Server's loop outside SketchUp, with stand-ins for the few
# SketchUp APIs it touches, and prints one JSON object of observations for
# tests/test_bridge_server.py to assert on. Plain Ruby, no gems.
#
# Usage: ruby tests/bridge_harness.rb su_mcp/su_mcp/main.rb

require 'socket'
require 'json'

module UI
  def self.start_timer(*) = 1
  def self.stop_timer(*) = nil
end

console = Object.new
def console.show; end
def console.write(msg) = ($bridge_log ||= []) << msg
SKETCHUP_CONSOLE = console

def file_loaded?(*) = true
def file_loaded(*) = nil
module Sketchup; end
$LOADED_FEATURES << 'sketchup.rb'

load ARGV.fetch(0)

# Short limits so the test runs in seconds.
SU_MCP::Server.send(:remove_const, :IDLE_TIMEOUT)
SU_MCP::Server.const_set(:IDLE_TIMEOUT, 0.5)
SU_MCP::Server.send(:remove_const, :MAX_REQUEST_BYTES)
SU_MCP::Server.const_set(:MAX_REQUEST_BYTES, 4096)

PORT = 19_000 + rand(900)
server = SU_MCP::Server.new
server.instance_variable_set(:@port, PORT)
server.start

$ticks = []
def tick(server)
  started = Time.now
  server.send(:tick)
  $ticks << (Time.now - started)
end

def request(code, id: 1)
  "#{JSON.generate({ jsonrpc: '2.0', method: 'tools/call', id: id,
                     params: { name: 'eval_ruby', arguments: { code: code } } })}\n"
end

# Ticks until the client has a full reply line (or the deadline), reading as it goes.
def reply_for(server, client, within: 3)
  buffer = +''
  deadline = Time.now + within
  while Time.now < deadline
    tick(server)
    chunk = client.read_nonblock(65_536, exception: false)
    return buffer if chunk.nil? # closed
    buffer << chunk if chunk.is_a?(String)
    return buffer if buffer.include?("\n")

    sleep 0.02
  end
  buffer
end

def closed_by_server?(client)
  client.read_nonblock(1, exception: false).nil?
rescue StandardError
  true
end

def run_until(server, seconds)
  deadline = Time.now + seconds
  while Time.now < deadline
    tick(server)
    sleep 0.02
  end
end

out = {}

# 1. A client that connects and says nothing must not hold up the next one.
silent = TCPSocket.new('127.0.0.1', PORT)
good = TCPSocket.new('127.0.0.1', PORT)
good.write(request('6 * 7'))
reply = reply_for(server, good)
out['served_while_silent_waits'] = reply.empty? ? nil : JSON.parse(reply).dig('result', 'content', 0, 'text')
out['good_closed_after_reply'] = closed_by_server?(good)

# 2. The silent client is dropped after the idle timeout; so is half a request.
partial = TCPSocket.new('127.0.0.1', PORT)
partial.write('{"jsonrpc":"2.0"')
run_until(server, 1.0)
out['silent_dropped'] = closed_by_server?(silent)
out['partial_dropped'] = closed_by_server?(partial)

# 3. A client still sending is not dropped: arriving bytes push the deadline back.
slow = TCPSocket.new('127.0.0.1', PORT)
line = request('1 + 1')
line.each_char.each_slice(8) do |piece|
  slow.write(piece.join)
  run_until(server, 0.1)
end
reply = reply_for(server, slow)
out['slow_sender_served'] = reply.empty? ? nil : JSON.parse(reply).dig('result', 'content', 0, 'text')

# 4. A syntax error in eval still gets an answer, and the socket is closed.
broken = TCPSocket.new('127.0.0.1', PORT)
broken.write(request('def x('))
reply = reply_for(server, broken)
parsed = reply.empty? ? {} : JSON.parse(reply)
out['syntax_error_answered'] = !parsed['error'].nil?
out['syntax_error_closed'] = closed_by_server?(broken)

# 5. An oversized request is dropped.
big = TCPSocket.new('127.0.0.1', PORT)
big.write('x' * 10_000)
run_until(server, 0.3)
out['oversized_dropped'] = closed_by_server?(big)

# 6. A client that never reads a large reply cannot block a tick, and is dropped.
stuck = TCPSocket.new('127.0.0.1', PORT)
stuck.setsockopt(Socket::SOL_SOCKET, Socket::SO_RCVBUF, 4096)
stuck.write(request('"x" * 20_000_000'))
# Building the 20 MB reply is the command's own cost; what must stay fast is
# every tick after it, while the reply sits unread.
tick(server) until server.instance_variable_get(:@outgoing).any?
$ticks.clear
run_until(server, 1.5)
out['slowest_tick_ms_with_stuck_reader'] = ($ticks.max * 1000).round(1)
out['stuck_reader_dropped'] = server.instance_variable_get(:@outgoing).empty?

# 7. Afterwards nothing is left pending.
out['pending_left'] = server.instance_variable_get(:@pending).size
server.stop
puts JSON.generate(out)
