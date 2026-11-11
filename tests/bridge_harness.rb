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
module Sketchup
  class AppObserver; end
end
$LOADED_FEATURES << 'sketchup.rb'

load ARGV.fetch(0)

# Short limits so the test runs in seconds.
SU_MCP::Server.send(:remove_const, :WRITE_TIMEOUT)
SU_MCP::Server.const_set(:WRITE_TIMEOUT, 0.5)
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

# The reply's text, or nil if there's no whole reply.
def text_of(reply)
  reply.empty? ? nil : JSON.parse(reply).dig('result', 'content', 0, 'text')
rescue JSON::ParserError
  nil
end

# Reads past anything the server sent (an error reply, say) to see whether
# the connection has been closed.
def closed_by_server?(client)
  loop do
    chunk = client.read_nonblock(65_536, exception: false)
    return true if chunk.nil?
    return false if chunk == :wait_readable
  end
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

def clients(server) = server.instance_variable_get(:@clients)

out = {}

# 1. A client that connects and says nothing must not hold up the next one,
#    and a client keeps its connection for further requests (the MCP server
#    sends a whole session's requests down one socket).
silent = TCPSocket.new('127.0.0.1', PORT)
good = TCPSocket.new('127.0.0.1', PORT)
good.write(request('6 * 7'))
out['served_while_silent_waits'] = text_of(reply_for(server, good))
good.write(request('2 + 3', id: 2))
out['second_request_on_same_socket'] = text_of(reply_for(server, good))

# 2. A client still sending is served once its request is complete.
slow = TCPSocket.new('127.0.0.1', PORT)
line = request('1 + 1')
line.each_char.each_slice(8) do |piece|
  slow.write(piece.join)
  run_until(server, 0.05)
end
out['slow_sender_served'] = text_of(reply_for(server, slow))

# 3. A syntax error in eval still gets an answer, and the socket stays usable.
broken = TCPSocket.new('127.0.0.1', PORT)
broken.write(request('def x('))
parsed = reply_for(server, broken).then { |r| r.empty? ? {} : JSON.parse(r) }
out['syntax_error_answered'] = !parsed['error'].nil?
broken.write(request(':ok', id: 3))
out['usable_after_syntax_error'] = text_of(reply_for(server, broken))

# 4. An oversized request is dropped.
big = TCPSocket.new('127.0.0.1', PORT)
big.write('x' * 10_000)
run_until(server, 0.3)
out['oversized_dropped'] = closed_by_server?(big)

# 5. A client that never reads a large reply cannot block a tick, and is dropped.
stuck = TCPSocket.new('127.0.0.1', PORT)
stuck.setsockopt(Socket::SOL_SOCKET, Socket::SO_RCVBUF, 4096)
stuck.write(request('"x" * 20_000_000'))
# Building the 20 MB reply is the command's own cost; what must stay fast is
# every tick after it, while the reply sits unread.
deadline = Time.now + 10
tick(server) until clients(server).any? { |c| !c[:out].empty? } || Time.now > deadline
stuck_entry = clients(server).find { |c| !c[:out].empty? }
$ticks.clear
run_until(server, 1.5)
out['slowest_tick_ms_with_stuck_reader'] = ($ticks.max * 1000).round(1)
out['stuck_reader_dropped'] = !stuck_entry.nil? && !clients(server).include?(stuck_entry)

# 5b. A client reading a large reply slowly but steadily gets all of it, even
#     when that takes longer than WRITE_TIMEOUT: the timeout counts from the
#     last time it took any.
steady = TCPSocket.new('127.0.0.1', PORT)
steady.setsockopt(Socket::SOL_SOCKET, Socket::SO_RCVBUF, 4096)
steady.write(request('"y" * 2_000_000'))
got = +''
started = Time.now
while Time.now - started < 20 && !got.include?("\n")
  tick(server)
  chunk = steady.read_nonblock(16_384, exception: false)
  break if chunk.nil?

  got << chunk if chunk.is_a?(String)
  sleep 0.01
end
out['steady_reader_seconds'] = (Time.now - started).round(2)
out['steady_reader_got_whole_reply'] = (text_of(got)&.length == 2_000_000)

# 5c. A client that sends its request and shuts down its write side still
#     gets the answer.
half = TCPSocket.new('127.0.0.1', PORT)
half.write(request('"half"'))
half.close_write
out['half_closed_served'] = text_of(reply_for(server, half))

# 5d. While a request is running (here, ticking the server itself, as a
#     nested event loop in SketchUp does), another client's tool call is
#     answered "busy" and not run, and server_status is answered.
other = TCPSocket.new('127.0.0.1', PORT)
$ran_inside = false
$nested_probe = lambda do
  other.write(request('$ran_inside = true', id: 10))
  busy = reply_for(server, other)
  other.write("#{JSON.generate({ jsonrpc: '2.0', method: 'tools/call', id: 11,
                                 params: { name: 'server_status', arguments: {} } })}\n")
  status = reply_for(server, other)
  [busy, status]
end
outer = TCPSocket.new('127.0.0.1', PORT)
outer.write(request('$nested = $nested_probe.call; :outer_done', id: 12))
out['outer_finished'] = text_of(reply_for(server, outer))
busy, status = $nested || ['', '']
out['nested_call_refused_busy'] = busy.empty? ? nil : JSON.parse(busy).dig('error', 'message').to_s.include?('busy')
out['nested_call_not_run'] = !$ran_inside
out['status_answered_while_busy'] = status.empty? ? nil : text_of(status).to_s.include?('in flight')

# 6. Clients that hang up are let go: one request per connection, as the
#    AutoClad scenario runner sends them, leaves nothing behind.
[silent, good, slow, broken, stuck, steady, half, other, outer].each { |s| s.close rescue nil }
once = TCPSocket.new('127.0.0.1', PORT)
once.write(request('"once"'))
out['one_shot_served'] = text_of(reply_for(server, once))
once.close
run_until(server, 0.3)
out['clients_left'] = clients(server).size
server.stop
puts JSON.generate(out)
