/* SPDX-License-Identifier: GPL-2.0-only
 * Wi-Fi infrastructure experiment for ns-3.35.
 * Supports baseline OnOff/PacketSink (schema v2) and custom binary-framed
 * ns3::Socket sender/receiver measuring true application E2E latency,
 * jitter, CRC32 integrity, out-of-order frames, TCP variants & RTS/CTS (schema v3).
 */
#include "ns3/core-module.h"
#include "ns3/network-module.h"
#include "ns3/internet-module.h"
#include "ns3/mobility-module.h"
#include "ns3/wifi-module.h"
#include "ns3/applications-module.h"
#include "ns3/flow-monitor-module.h"
#include "ns3/netanim-module.h"
#include <algorithm>
#include <cmath>
#include <cstdint>
#include <cstring>
#include <fstream>
#include <iomanip>
#include <map>
#include <memory>
#include <sstream>
#include <vector>
#ifdef __linux__
#include <arpa/inet.h>
#include <fcntl.h>
#include <sys/socket.h>
#include <unistd.h>
#endif

using namespace ns3;

namespace {

constexpr uint16_t kN44HeaderBytes = 24;
constexpr uint8_t kN44Magic0 = 'N';
constexpr uint8_t kN44Magic1 = '4';
constexpr uint8_t kN44Version = 1;
constexpr uint8_t kN44MsgData = 1;

uint32_t ComputeCrc32 (const uint8_t *data, size_t len, uint32_t crc = 0xFFFFFFFFu)
{
  for (size_t i = 0; i < len; ++i)
    {
      crc ^= static_cast<uint32_t> (data[i]);
      for (int b = 0; b < 8; ++b)
        {
          uint32_t mask = -(crc & 1u);
          crc = (crc >> 1) ^ (0xEDB88320u & mask);
        }
    }
  return crc;
}

uint32_t ComputeN44FrameCrc (const uint8_t *frame, uint16_t payloadLen)
{
  uint32_t crc = ComputeCrc32 (frame, 20, 0xFFFFFFFFu);
  crc = ComputeCrc32 (frame + kN44HeaderBytes, payloadLen, crc);
  return ~crc;
}

inline void WriteU16BE (uint8_t *p, uint16_t v)
{
  p[0] = static_cast<uint8_t> ((v >> 8) & 0xFF);
  p[1] = static_cast<uint8_t> (v & 0xFF);
}
inline void WriteU32BE (uint8_t *p, uint32_t v)
{
  p[0] = static_cast<uint8_t> ((v >> 24) & 0xFF);
  p[1] = static_cast<uint8_t> ((v >> 16) & 0xFF);
  p[2] = static_cast<uint8_t> ((v >> 8) & 0xFF);
  p[3] = static_cast<uint8_t> (v & 0xFF);
}
inline void WriteU64BE (uint8_t *p, uint64_t v)
{
  for (int i = 0; i < 8; ++i)
    p[i] = static_cast<uint8_t> ((v >> (56 - 8 * i)) & 0xFF);
}
inline uint16_t ReadU16BE (const uint8_t *p)
{
  return static_cast<uint16_t> ((static_cast<uint16_t> (p[0]) << 8) | p[1]);
}
inline uint32_t ReadU32BE (const uint8_t *p)
{
  return (static_cast<uint32_t> (p[0]) << 24) |
         (static_cast<uint32_t> (p[1]) << 16) |
         (static_cast<uint32_t> (p[2]) << 8) |
         static_cast<uint32_t> (p[3]);
}
inline uint64_t ReadU64BE (const uint8_t *p)
{
  uint64_t v = 0;
  for (int i = 0; i < 8; ++i)
    v = (v << 8) | static_cast<uint64_t> (p[i]);
  return v;
}

struct AppCount
{
  uint64_t txBytes = 0, rxBytes = 0, rxActive = 0;
  uint64_t txFrames = 0, rxFrames = 0, crcOkFrames = 0, crcBadFrames = 0, outOfOrderFrames = 0;
  uint64_t expectedSeq = 0;
  uint64_t delayNsSum = 0, jitterNsSum = 0, jitterSamples = 0;
  bool hasLastDelay = false;
  int64_t lastDelayNs = 0;
};

static std::vector<AppCount> counts;
static std::vector<bool> staReady, apReady;
static double sendStart, sendStop;
static std::vector<Ptr<Node>> g_staNodes;

#ifdef __linux__
static int g_liveSock = -1;
static sockaddr_in g_liveAddr {};
static double g_lastLiveForward = -1.0;
#endif

static void ForwardLiveTelemetry (uint16_t stationId, uint32_t seqNum, uint64_t txTimeNs, uint32_t frameBytes, bool crcOk)
{
#ifdef __linux__
  if (g_liveSock < 0) return;
  double nowS = Simulator::Now ().GetSeconds ();
  if (nowS - g_lastLiveForward < 0.02) return; // Cap forwarding at ~50 events/s sim-time
  g_lastLiveForward = nowS;
  double x = 0.0, y = 0.0;
  if (stationId < g_staNodes.size () && g_staNodes[stationId])
    {
      Vector pos = g_staNodes[stationId]->GetObject<MobilityModel> ()->GetPosition ();
      x = pos.x;
      y = pos.y;
    }
  double delayMs = static_cast<double> (Simulator::Now ().GetNanoSeconds () - static_cast<int64_t> (txTimeNs)) / 1e6;
  std::ostringstream js;
  js << std::fixed << std::setprecision (3)
     << "{\"sta\":" << stationId << ",\"seq\":" << seqNum << ",\"delay_ms\":" << delayMs
     << ",\"x\":" << x << ",\"y\":" << y << ",\"t\":" << nowS << ",\"crc\":" << (crcOk ? 1 : 0) << "}";
  std::string body = js.str ();
  std::vector<uint8_t> wire (kN44HeaderBytes + body.size (), 0);
  wire[0] = kN44Magic0; wire[1] = kN44Magic1; wire[2] = kN44Version; wire[3] = kN44MsgData;
  WriteU16BE (&wire[4], stationId);
  WriteU16BE (&wire[6], static_cast<uint16_t> (body.size ()));
  WriteU32BE (&wire[8], seqNum);
  WriteU64BE (&wire[12], txTimeNs);
  std::memcpy (&wire[kN44HeaderBytes], body.data (), body.size ());
  WriteU32BE (&wire[20], ComputeN44FrameCrc (wire.data (), static_cast<uint16_t> (body.size ())));
  (void) sendto (g_liveSock, wire.data (), wire.size (), 0, reinterpret_cast<sockaddr *> (&g_liveAddr), sizeof (g_liveAddr));
#else
  (void) stationId; (void) seqNum; (void) txTimeNs; (void) frameBytes; (void) crcOk;
#endif
}

static void ObserveAssociation (NetDeviceContainer staDev, NetDeviceContainer apDev)
{
  Ptr<WifiNetDevice> apWifi = DynamicCast<WifiNetDevice> (apDev.Get (0));
  for (uint32_t i = 0; i < staDev.GetN (); ++i)
    {
      Ptr<WifiNetDevice> device = DynamicCast<WifiNetDevice> (staDev.Get (i));
      staReady[i] = DynamicCast<StaWifiMac> (device->GetMac ())->IsAssociated ();
      apReady[i] = apWifi->GetRemoteStationManager ()->IsAssociated (
          Mac48Address::ConvertFrom (staDev.Get (i)->GetAddress ()));
    }
}

// OnOff Tx fires only when Socket::Send accepted the complete write.
static void AppTx (uint32_t i, Ptr<const Packet> p)
{
  counts.at (i).txBytes += p->GetSize ();
}
static void AppRx (uint32_t i, Ptr<const Packet> p, const Address &)
{
  counts.at (i).rxBytes += p->GetSize ();
  double t = Simulator::Now ().GetSeconds ();
  if (t >= sendStart && t < sendStop)
    counts.at (i).rxActive += p->GetSize ();
}

class CustomSocketSender : public Application
{
public:
  void Setup (TypeId tid, Address peer, uint16_t stationId, uint32_t frameSize, double rateMbps)
  {
    m_tid = tid;
    m_peer = peer;
    m_stationId = stationId;
    m_frameSize = frameSize;
    m_rateMbps = rateMbps;
    m_isTcp = (tid == TcpSocketFactory::GetTypeId ());
  }

protected:
  void DoDispose () override
  {
    m_socket = nullptr;
    Application::DoDispose ();
  }

private:
  void StartApplication () override
  {
    m_running = true;
    m_connected = !m_isTcp;
    m_socket = Socket::CreateSocket (GetNode (), m_tid);
    m_socket->SetSendCallback (MakeCallback (&CustomSocketSender::HandleSend, this));
    if (m_isTcp)
      {
        m_socket->SetConnectCallback (
            MakeCallback (&CustomSocketSender::HandleConnectOk, this),
            MakeNullCallback<void, Ptr<Socket>> ());
      }
    m_socket->Bind ();
    m_socket->Connect (m_peer);
    if (!m_isTcp)
      {
        m_sendEvent = Simulator::ScheduleNow (&CustomSocketSender::TrySend, this);
      }
  }

  void StopApplication () override
  {
    m_running = false;
    Simulator::Cancel (m_sendEvent);
    if (m_socket)
      {
        m_socket->Close ();
      }
  }

  void HandleConnectOk (Ptr<Socket>)
  {
    m_connected = true;
    if (m_running && !m_sendEvent.IsRunning ())
      {
        m_sendEvent = Simulator::ScheduleNow (&CustomSocketSender::TrySend, this);
      }
  }

  void HandleSend (Ptr<Socket>, uint32_t)
  {
    if (m_running && m_waitingTx && m_connected)
      {
        m_waitingTx = false;
        TrySend ();
      }
  }

  void TrySend ()
  {
    if (!m_running || !m_connected || !m_socket) return;
    if (m_socket->GetTxAvailable () < m_frameSize)
      {
        m_waitingTx = true;
        return;
      }

    uint16_t payloadLen = static_cast<uint16_t> (m_frameSize - kN44HeaderBytes);
    std::vector<uint8_t> buf (m_frameSize, 0x20);
    buf[0] = kN44Magic0;
    buf[1] = kN44Magic1;
    buf[2] = kN44Version;
    buf[3] = kN44MsgData;
    WriteU16BE (&buf[4], m_stationId);
    WriteU16BE (&buf[6], payloadLen);
    WriteU32BE (&buf[8], m_seqNum);
    WriteU64BE (&buf[12], static_cast<uint64_t> (Simulator::Now ().GetNanoSeconds ()));
    std::ostringstream tag;
    tag << "N44;STA=" << m_stationId << ";SEQ=" << m_seqNum << ';';
    std::string tagStr = tag.str ();
    size_t copyLen = std::min<size_t> (tagStr.size (), payloadLen);
    std::memcpy (&buf[kN44HeaderBytes], tagStr.data (), copyLen);
    WriteU32BE (&buf[20], ComputeN44FrameCrc (buf.data (), payloadLen));

    Ptr<Packet> pkt = Create<Packet> (buf.data (), m_frameSize);
    int sent = m_socket->Send (pkt);
    if (sent == static_cast<int> (m_frameSize))
      {
        counts.at (m_stationId).txBytes += m_frameSize;
        counts.at (m_stationId).txFrames += 1;
        ++m_seqNum;
        Time nextGap = Seconds (static_cast<double> (m_frameSize) * 8.0 / (m_rateMbps * 1e6));
        m_sendEvent = Simulator::Schedule (nextGap, &CustomSocketSender::TrySend, this);
      }
    else
      {
        m_waitingTx = true;
      }
  }

  TypeId m_tid;
  Address m_peer;
  uint16_t m_stationId = 0;
  uint32_t m_frameSize = 1024;
  double m_rateMbps = 1.0;
  bool m_isTcp = false;
  bool m_running = false;
  bool m_connected = false;
  bool m_waitingTx = false;
  uint32_t m_seqNum = 0;
  Ptr<Socket> m_socket;
  EventId m_sendEvent;
};

class CustomSocketReceiver : public Application
{
public:
  void Setup (TypeId tid, uint16_t port, uint32_t stationId)
  {
    m_tid = tid;
    m_port = port;
    m_stationId = stationId;
    m_isTcp = (tid == TcpSocketFactory::GetTypeId ());
  }

protected:
  void DoDispose () override
  {
    m_socket = nullptr;
    m_tcpBuffers.clear ();
    Application::DoDispose ();
  }

private:
  void StartApplication () override
  {
    m_socket = Socket::CreateSocket (GetNode (), m_tid);
    InetSocketAddress local (Ipv4Address::GetAny (), m_port);
    m_socket->Bind (local);
    if (m_isTcp)
      {
        m_socket->Listen ();
        m_socket->SetAcceptCallback (
            MakeNullCallback<bool, Ptr<Socket>, const Address &> (),
            MakeCallback (&CustomSocketReceiver::HandleAccept, this));
      }
    else
      {
        m_socket->SetRecvCallback (MakeCallback (&CustomSocketReceiver::HandleReadUdp, this));
      }
  }

  void StopApplication () override
  {
    if (m_socket) m_socket->Close ();
    for (auto &kv : m_tcpBuffers)
      {
        if (kv.first) kv.first->Close ();
      }
  }

  void HandleAccept (Ptr<Socket> sock, const Address &)
  {
    m_tcpBuffers[sock] = std::vector<uint8_t> ();
    sock->SetRecvCallback (MakeCallback (&CustomSocketReceiver::HandleReadTcp, this));
  }

  void HandleReadUdp (Ptr<Socket> sock)
  {
    Ptr<Packet> pkt;
    Address from;
    while ((pkt = sock->RecvFrom (from)) && pkt->GetSize () > 0)
      {
        std::vector<uint8_t> buf (pkt->GetSize ());
        pkt->CopyData (buf.data (), static_cast<uint32_t> (buf.size ()));
        ConsumeFrames (buf);
      }
  }

  void HandleReadTcp (Ptr<Socket> sock)
  {
    auto &streamBuf = m_tcpBuffers[sock];
    Ptr<Packet> pkt;
    while ((pkt = sock->Recv ()) && pkt->GetSize () > 0)
      {
        size_t oldSize = streamBuf.size ();
        streamBuf.resize (oldSize + pkt->GetSize ());
        pkt->CopyData (streamBuf.data () + oldSize, static_cast<uint32_t> (pkt->GetSize ()));
      }
    ConsumeFrames (streamBuf);
  }

  void ConsumeFrames (std::vector<uint8_t> &buf)
  {
    size_t offset = 0;
    while (buf.size () - offset >= kN44HeaderBytes)
      {
        const uint8_t *hdr = buf.data () + offset;
        if (hdr[0] != kN44Magic0 || hdr[1] != kN44Magic1 || hdr[2] != kN44Version)
          {
            ++offset;
            continue;
          }
        uint16_t payloadLen = ReadU16BE (hdr + 6);
        size_t frameLen = static_cast<size_t> (kN44HeaderBytes) + payloadLen;
        if (buf.size () - offset < frameLen)
          {
            break; // Wait for remaining TCP stream bytes
          }
        uint16_t staId = ReadU16BE (hdr + 4);
        uint32_t seqNum = ReadU32BE (hdr + 8);
        uint64_t txTimeNs = ReadU64BE (hdr + 12);
        uint32_t rxCrc = ReadU32BE (hdr + 20);
        uint32_t expectedCrc = ComputeN44FrameCrc (hdr, payloadLen);
        bool crcOk = (rxCrc == expectedCrc);

        uint32_t idx = (staId < counts.size ()) ? staId : m_stationId;
        auto &a = counts.at (idx);
        a.rxFrames += 1;
        a.rxBytes += frameLen;
        double nowS = Simulator::Now ().GetSeconds ();
        if (nowS >= sendStart && nowS < sendStop)
          {
            a.rxActive += frameLen;
          }
        if (crcOk)
          a.crcOkFrames += 1;
        else
          a.crcBadFrames += 1;
        if (seqNum < a.expectedSeq)
          a.outOfOrderFrames += 1;
        else
          a.expectedSeq = seqNum + 1;

        int64_t delayNs = static_cast<int64_t> (Simulator::Now ().GetNanoSeconds ()) - static_cast<int64_t> (txTimeNs);
        if (delayNs >= 0)
          {
            a.delayNsSum += static_cast<uint64_t> (delayNs);
            if (a.hasLastDelay)
              {
                int64_t diff = delayNs - a.lastDelayNs;
                a.jitterNsSum += static_cast<uint64_t> (diff >= 0 ? diff : -diff);
                a.jitterSamples += 1;
              }
            a.lastDelayNs = delayNs;
            a.hasLastDelay = true;
          }
        ForwardLiveTelemetry (static_cast<uint16_t> (idx), seqNum, txTimeNs, static_cast<uint32_t> (frameLen), crcOk);
        offset += frameLen;
      }
    if (offset > 0)
      {
        buf.erase (buf.begin (), buf.begin () + offset);
      }
  }

  TypeId m_tid;
  uint16_t m_port = 9000;
  uint32_t m_stationId = 0;
  bool m_isTcp = false;
  Ptr<Socket> m_socket;
  std::map<Ptr<Socket>, std::vector<uint8_t>> m_tcpBuffers;
};

} // namespace

static std::string NumberOrBlank (double numerator, uint64_t denominator)
{
  if (!denominator) return "";
  std::ostringstream s;
  s << std::setprecision (12) << numerator / denominator;
  return s.str ();
}
static void OpenOutput (std::ofstream &f, const std::string &path)
{
  f.open (path.c_str ());
  NS_ABORT_MSG_IF (!f, "Cannot write " << path << "; create its parent directory first.");
  f << std::setprecision (12);
}
struct IpCount
{
  uint64_t tx = 0, rx = 0, txBytes = 0, rxBytes = 0, lostMonitor = 0;
  double delay = 0;
};

int main (int argc, char *argv[])
{
  uint32_t stations = 5, seed = 12345, packetSize = 1024, liveUdpPort = 0;
  uint64_t run = 1;
  double duration = 30, cleanup = 10, start = 3, halfSide = 25, speed = 1.5;
  double rateMbps = 1.0, txPower = 16;
  bool rtsCts = false, xml = false, pcap = false, visual = false;
  std::string transport = "udp", mobility = "moving", output = "wifi-result";
  std::string appMode = "onoff", tcpVariant = "NewReno", liveUdpHost = "127.0.0.1";
  CommandLine cmd (__FILE__);
  cmd.AddValue ("stations", "Number of sending STAs, excluding AP (1..200)", stations);
  cmd.AddValue ("transport", "udp or tcp", transport);
  cmd.AddValue ("appMode", "onoff (schema v2) or socket (24B binary header, schema v3)", appMode);
  cmd.AddValue ("tcpVariant", "TCP congestion control: NewReno, Cubic, or Bbr", tcpVariant);
  cmd.AddValue ("rtsCts", "Enable RTS/CTS (threshold=500 bytes) when true", rtsCts);
  cmd.AddValue ("seed", "RNG seed", seed);
  cmd.AddValue ("run", "Independent RNG run number", run);
  cmd.AddValue ("duration", "Common application sending window, seconds", duration);
  cmd.AddValue ("cleanup", "Drain interval after sources stop, seconds", cleanup);
  cmd.AddValue ("start", "Common source start time, seconds", start);
  cmd.AddValue ("rateMbps", "Requested application rate per station, decimal Mbps", rateMbps);
  cmd.AddValue ("packetSize", "Application write/frame size, bytes (64..1400)", packetSize);
  cmd.AddValue ("halfSide", "Area is [-halfSide,+halfSide]^2 metres", halfSide);
  cmd.AddValue ("speed", "RandomWalk speed, m/s", speed);
  cmd.AddValue ("mobility", "moving or static (static is diagnostic baseline)", mobility);
  cmd.AddValue ("txPower", "Transmit power, dBm", txPower);
  cmd.AddValue ("output", "Output path prefix; parent directory must exist", output);
  cmd.AddValue ("xml", "Also save raw FlowMonitor XML", xml);
  cmd.AddValue ("pcap", "Also save AP pcap for diagnostics", pcap);
  cmd.AddValue ("visual", "Save labeled NetAnim replay XML", visual);
  cmd.AddValue ("liveUdpHost", "Optional Linux UDP telemetry target host", liveUdpHost);
  cmd.AddValue ("liveUdpPort", "Optional Linux UDP telemetry target port (0=disabled)", liveUdpPort);
  cmd.Parse (argc, argv);

  NS_ABORT_MSG_IF (stations < 1 || stations > 200, "stations must be 1..200");
  NS_ABORT_MSG_IF (transport != "tcp" && transport != "udp", "transport must be tcp/udp");
  NS_ABORT_MSG_IF (appMode != "onoff" && appMode != "socket", "appMode must be onoff or socket");
  NS_ABORT_MSG_IF (tcpVariant != "NewReno" && tcpVariant != "Cubic" && tcpVariant != "Bbr",
                   "tcpVariant must be NewReno, Cubic, or Bbr");
  NS_ABORT_MSG_IF (mobility != "moving" && mobility != "static", "mobility must be moving/static");
  NS_ABORT_MSG_IF (!seed || !run, "seed and run must be positive");
  NS_ABORT_MSG_IF (!std::isfinite (duration) || duration <= 0 ||
                   !std::isfinite (cleanup) || cleanup < 0 ||
                   !std::isfinite (start) || start < 1 ||
                   !std::isfinite (rateMbps) || rateMbps <= 0 || rateMbps > 100 ||
                   !std::isfinite (halfSide) || halfSide <= 0 ||
                   !std::isfinite (speed) || speed <= 0 ||
                   !std::isfinite (txPower), "Invalid numeric parameter");
  NS_ABORT_MSG_IF (packetSize < 64 || packetSize > 1400, "Use packetSize 64..1400 to avoid IPv4 fragmentation");

  sendStart = start;
  sendStop = start + duration;
  const double simStop = sendStop + cleanup;
  RngSeedManager::SetSeed (seed);
  RngSeedManager::SetRun (run);

  const std::string tcpTypeName = "ns3::Tcp" + tcpVariant;
  TypeId tcpTid;
  NS_ABORT_MSG_UNLESS (TypeId::LookupByNameFailSafe (tcpTypeName, &tcpTid),
                       "TypeId " << tcpTypeName << " not found");
  Config::SetDefault ("ns3::TcpL4Protocol::SocketType", TypeIdValue (tcpTid));
  Config::SetDefault ("ns3::TcpSocket::SegmentSize", UintegerValue (packetSize));

#ifdef __linux__
  if (liveUdpPort > 0)
    {
      g_liveSock = socket (AF_INET, SOCK_DGRAM, 0);
      if (g_liveSock >= 0)
        {
          fcntl (g_liveSock, F_SETFL, O_NONBLOCK);
          g_liveAddr.sin_family = AF_INET;
          g_liveAddr.sin_port = htons (static_cast<uint16_t> (liveUdpPort));
          inet_pton (AF_INET, liveUdpHost.c_str (), &g_liveAddr.sin_addr);
        }
    }
#endif

  NodeContainer sta, ap;
  sta.Create (stations);
  ap.Create (1);
  NodeContainer all;
  all.Add (sta);
  all.Add (ap);
  g_staNodes.resize (stations);
  for (uint32_t i = 0; i < stations; ++i) g_staNodes[i] = sta.Get (i);

  YansWifiChannelHelper channel;
  channel.SetPropagationDelay ("ns3::ConstantSpeedPropagationDelayModel");
  channel.AddPropagationLoss ("ns3::LogDistancePropagationLossModel",
      "Exponent", DoubleValue (3.0), "ReferenceDistance", DoubleValue (1.0),
      "ReferenceLoss", DoubleValue (46.6777));
  YansWifiPhyHelper phy;
  phy.SetChannel (channel.Create ());
  phy.Set ("TxPowerStart", DoubleValue (txPower));
  phy.Set ("TxPowerEnd", DoubleValue (txPower));
  WifiHelper wifi;
  wifi.SetStandard (WIFI_STANDARD_80211a);
  wifi.SetRemoteStationManager ("ns3::ConstantRateWifiManager",
      "DataMode", StringValue ("OfdmRate24Mbps"),
      "ControlMode", StringValue ("OfdmRate6Mbps"),
      "RtsCtsThreshold", UintegerValue (rtsCts ? 500 : 65535));
  WifiMacHelper mac;
  Ssid ssid ("wifi-transport-study");
  mac.SetType ("ns3::StaWifiMac", "Ssid", SsidValue (ssid),
               "ActiveProbing", BooleanValue (false), "QosSupported", BooleanValue (false));
  NetDeviceContainer staDev = wifi.Install (phy, mac, sta);
  mac.SetType ("ns3::ApWifiMac", "Ssid", SsidValue (ssid),
               "QosSupported", BooleanValue (false));
  NetDeviceContainer apDev = wifi.Install (phy, mac, ap);
  NetDeviceContainer devices;
  devices.Add (staDev);
  devices.Add (apDev);
  wifi.AssignStreams (devices, 10000);

  Ptr<UniformRandomVariable> pos = CreateObject<UniformRandomVariable> ();
  pos->SetStream (1);
  Ptr<ListPositionAllocator> positions = CreateObject<ListPositionAllocator> ();
  std::vector<Vector> initial;
  for (uint32_t i = 0; i < stations; ++i)
    {
      Vector v (pos->GetValue (-halfSide * 0.99, halfSide * 0.99),
                pos->GetValue (-halfSide * 0.99, halfSide * 0.99), 0);
      initial.push_back (v);
      positions->Add (v);
    }
  MobilityHelper move;
  move.SetPositionAllocator (positions);
  std::ostringstream speedRv;
  speedRv << "ns3::ConstantRandomVariable[Constant=" << speed << "]";
  if (mobility == "moving")
    move.SetMobilityModel ("ns3::RandomWalk2dMobilityModel",
        "Mode", StringValue ("Time"), "Time", TimeValue (Seconds (1)),
        "Speed", StringValue (speedRv.str ()),
        "Bounds", RectangleValue (Rectangle (-halfSide, halfSide, -halfSide, halfSide)));
  else
    move.SetMobilityModel ("ns3::ConstantPositionMobilityModel");
  move.Install (sta);
  move.AssignStreams (sta, 100);
  MobilityHelper fixed;
  fixed.SetMobilityModel ("ns3::ConstantPositionMobilityModel");
  fixed.Install (ap);
  ap.Get (0)->GetObject<MobilityModel> ()->SetPosition (Vector (0, 0, 0));

  InternetStackHelper internet;
  internet.Install (all);
  Ipv4AddressHelper address;
  address.SetBase ("10.1.1.0", "255.255.255.0");
  Ipv4InterfaceContainer staIp = address.Assign (staDev);
  Ipv4InterfaceContainer apIp = address.Assign (apDev);
  staReady.assign (stations, false);
  apReady.assign (stations, false);
  Simulator::Schedule (Seconds (sendStart) - NanoSeconds (1), &ObserveAssociation, staDev, apDev);

  const TypeId sockTid = (transport == "tcp") ? TcpSocketFactory::GetTypeId () : UdpSocketFactory::GetTypeId ();
  const std::string factory = (transport == "tcp") ? "ns3::TcpSocketFactory" : "ns3::UdpSocketFactory";
  counts.assign (stations, AppCount ());

  for (uint32_t i = 0; i < stations; ++i)
    {
      uint16_t port = 9000 + i;
      if (appMode == "onoff")
        {
          PacketSinkHelper sink (factory, InetSocketAddress (Ipv4Address::GetAny (), port));
          ApplicationContainer receivers = sink.Install (ap.Get (0));
          receivers.Start (Seconds (0));
          receivers.Get (0)->TraceConnectWithoutContext ("Rx", MakeBoundCallback (&AppRx, i));

          OnOffHelper source (factory, InetSocketAddress (apIp.GetAddress (0), port));
          source.SetAttribute ("DataRate", DataRateValue (DataRate (static_cast<uint64_t> (rateMbps * 1e6))));
          source.SetAttribute ("PacketSize", UintegerValue (packetSize));
          source.SetAttribute ("OnTime", StringValue ("ns3::ConstantRandomVariable[Constant=1]"));
          source.SetAttribute ("OffTime", StringValue ("ns3::ConstantRandomVariable[Constant=0]"));
          ApplicationContainer senders = source.Install (sta.Get (i));
          source.AssignStreams (sta.Get (i), 20000 + 2 * i);
          senders.Start (Seconds (sendStart));
          senders.Stop (Seconds (sendStop));
          senders.Get (0)->TraceConnectWithoutContext ("Tx", MakeBoundCallback (&AppTx, i));
        }
      else
        {
          Ptr<CustomSocketReceiver> rxApp = CreateObject<CustomSocketReceiver> ();
          rxApp->Setup (sockTid, port, i);
          ap.Get (0)->AddApplication (rxApp);
          rxApp->SetStartTime (Seconds (0));

          Ptr<CustomSocketSender> txApp = CreateObject<CustomSocketSender> ();
          txApp->Setup (sockTid, InetSocketAddress (apIp.GetAddress (0), port),
                        static_cast<uint16_t> (i), packetSize, rateMbps);
          sta.Get (i)->AddApplication (txApp);
          txApp->SetStartTime (Seconds (sendStart));
          txApp->SetStopTime (Seconds (sendStop));
        }
    }

  if (pcap)
    {
      phy.SetPcapDataLinkType (WifiPhyHelper::DLT_IEEE802_11_RADIO);
      phy.EnablePcap (output, apDev.Get (0));
    }

  std::unique_ptr<AnimationInterface> animation;
  if (visual)
    {
      animation.reset (new AnimationInterface (output + "-animation.xml"));
      animation->SetMobilityPollInterval (Seconds (0.1));
      animation->EnablePacketMetadata (true);
      for (uint32_t i = 0; i < stations; ++i)
        {
          const uint32_t id = sta.Get (i)->GetId ();
          animation->UpdateNodeColor (id, 35, 120, 220);
          animation->UpdateNodeSize (id, 1.4, 1.4);
          animation->UpdateNodeDescription (id, "STA " + std::to_string (i) + " - SEND");
        }
      const uint32_t apId = ap.Get (0)->GetId ();
      animation->UpdateNodeColor (apId, 220, 55, 55);
      animation->UpdateNodeSize (apId, 2.4, 2.4);
      animation->UpdateNodeDescription (apId, "AP - RECEIVE");
    }

  FlowMonitorHelper flowHelper;
  Ptr<FlowMonitor> monitor = flowHelper.InstallAll ();
  Simulator::Stop (Seconds (simStop));
  Simulator::Run ();
  monitor->CheckForLostPackets ();

  Ptr<Ipv4FlowClassifier> classifier = DynamicCast<Ipv4FlowClassifier> (flowHelper.GetClassifier ());
  std::vector<IpCount> perStation (stations);
  for (const auto &entry : monitor->GetFlowStats ())
    {
      Ipv4FlowClassifier::FiveTuple t = classifier->FindFlow (entry.first);
      if (t.protocol != (transport == "tcp" ? 6 : 17) ||
          t.destinationAddress != apIp.GetAddress (0) ||
          t.destinationPort < 9000 || t.destinationPort >= 9000 + stations) continue;
      uint32_t i = t.destinationPort - 9000;
      if (t.sourceAddress != staIp.GetAddress (i)) continue;
      const auto &s = entry.second;
      auto &p = perStation.at (i);
      p.tx += s.txPackets; p.rx += s.rxPackets;
      p.txBytes += s.txBytes; p.rxBytes += s.rxBytes;
      p.lostMonitor += s.lostPackets; p.delay += s.delaySum.GetSeconds ();
    }

  const bool useV3 = (appMode == "socket") || (tcpVariant != "NewReno") || rtsCts;
  std::ofstream flows, summary, positionFile;
  OpenOutput (flows, output + "-flows.csv");
  OpenOutput (summary, output + "-summary.csv");
  OpenOutput (positionFile, output + "-positions.csv");
  std::string metrics = "tx_ip_packets,rx_ip_packets,unreceived_ip_packets,pdr_ip_pct,loss_ip_pct,delay_ip_ms,rx_ip_bytes,throughput_ip_kbps,app_tx_accepted_bytes,app_rx_bytes,app_rx_active_bytes,goodput_active_kbps,goodput_full_kbps,flowmonitor_lost_packets";
  if (useV3)
    {
      metrics += ",app_frames_tx,app_frames_rx,app_frames_crc_ok,app_frames_out_of_order,app_pdr_pct,app_delay_ms,app_jitter_ms";
    }
  flows << "station,source_ip,dest_ip,dest_port,sta_associated_at_start,ap_associated_at_start," << metrics << "\n";

  auto writeMetrics = [&] (std::ostream &f, const IpCount &p, const AppCount &a)
    {
      NS_ABORT_MSG_IF (p.rx > p.tx, "Invalid IP counts");
      f << p.tx << ',' << p.rx << ',' << p.tx - p.rx << ','
        << NumberOrBlank (100.0 * p.rx, p.tx) << ','
        << NumberOrBlank (100.0 * (p.tx - p.rx), p.tx) << ','
        << NumberOrBlank (1000.0 * p.delay, p.rx) << ','
        << p.rxBytes << ',' << p.rxBytes * 8.0 / ((duration + cleanup) * 1000) << ','
        << a.txBytes << ',' << a.rxBytes << ',' << a.rxActive << ','
        << a.rxActive * 8.0 / (duration * 1000) << ','
        << a.rxBytes * 8.0 / ((duration + cleanup) * 1000) << ',' << p.lostMonitor;
      if (useV3)
        {
          f << ',' << a.txFrames << ',' << a.rxFrames << ',' << a.crcOkFrames << ',' << a.outOfOrderFrames << ','
            << NumberOrBlank (100.0 * a.rxFrames, a.txFrames) << ','
            << NumberOrBlank (static_cast<double> (a.delayNsSum) / 1e6, a.rxFrames) << ','
            << NumberOrBlank (static_cast<double> (a.jitterNsSum) / 1e6, a.jitterSamples);
        }
    };

  IpCount total;
  AppCount appTotal;
  uint32_t zeroRx = 0, zeroTx = 0, zeroAppRx = 0, readyAtStart = 0, assocMismatch = 0;
  double sumGoodput = 0.0, sumGoodputSq = 0.0;
  positionFile << "station,x_start_m,y_start_m,x_end_m,y_end_m\n";
  for (uint32_t i = 0; i < stations; ++i)
    {
      const auto &p = perStation[i]; const auto &a = counts[i];
      flows << i << ',' << staIp.GetAddress (i) << ',' << apIp.GetAddress (0) << ',' << 9000 + i << ',';
      flows << staReady[i] << ',' << apReady[i] << ',';
      writeMetrics (flows, p, a); flows << '\n';

      total.tx += p.tx; total.rx += p.rx; total.txBytes += p.txBytes;
      total.rxBytes += p.rxBytes; total.delay += p.delay; total.lostMonitor += p.lostMonitor;
      appTotal.txBytes += a.txBytes; appTotal.rxBytes += a.rxBytes; appTotal.rxActive += a.rxActive;
      appTotal.txFrames += a.txFrames; appTotal.rxFrames += a.rxFrames;
      appTotal.crcOkFrames += a.crcOkFrames; appTotal.outOfOrderFrames += a.outOfOrderFrames;
      appTotal.delayNsSum += a.delayNsSum; appTotal.jitterNsSum += a.jitterNsSum;
      appTotal.jitterSamples += a.jitterSamples;

      double gActive = a.rxActive * 8.0 / (duration * 1000);
      sumGoodput += gActive;
      sumGoodputSq += gActive * gActive;

      zeroRx += (p.rx == 0); zeroTx += (p.tx == 0);
      zeroAppRx += (a.rxBytes == 0);
      readyAtStart += (staReady[i] && apReady[i]);
      assocMismatch += (staReady[i] != apReady[i]);
      Vector v = sta.Get (i)->GetObject<MobilityModel> ()->GetPosition ();
      positionFile << i << ',' << initial[i].x << ',' << initial[i].y << ',' << v.x << ',' << v.y << '\n';
    }
  double jainsFairness = (sumGoodputSq > 0.0) ? (sumGoodput * sumGoodput) / (stations * sumGoodputSq) : 0.0;

  if (!useV3)
    {
      summary << "schema_version,transport,tcp_variant,stations,seed,run,mobility,start_s,duration_s,cleanup_s,rate_mbps_per_sta,packet_size,half_side_m,speed_mps,tx_power_dbm,phy_mode,"
              << metrics << ",zero_tx_flows,zero_rx_flows,zero_app_rx_flows,associated_pairs_at_start,association_mismatches_at_start\n";
      summary << "2," << transport << "," << tcpVariant << "," << stations << ',' << seed << ',' << run << ',' << mobility << ','
              << start << ',' << duration << ',' << cleanup << ',' << rateMbps << ',' << packetSize << ','
              << halfSide << ',' << speed << ',' << txPower << ",OfdmRate24Mbps,";
      writeMetrics (summary, total, appTotal);
      summary << ',' << zeroTx << ',' << zeroRx << ',' << zeroAppRx << ',' << readyAtStart << ',' << assocMismatch << '\n';
    }
  else
    {
      summary << "schema_version,transport,tcp_variant,app_mode,rts_cts,stations,seed,run,mobility,start_s,duration_s,cleanup_s,rate_mbps_per_sta,packet_size,half_side_m,speed_mps,tx_power_dbm,phy_mode,"
              << metrics << ",jains_fairness_active,zero_tx_flows,zero_rx_flows,zero_app_rx_flows,associated_pairs_at_start,association_mismatches_at_start\n";
      summary << "3," << transport << "," << tcpVariant << "," << appMode << "," << (rtsCts ? 1 : 0) << ","
              << stations << ',' << seed << ',' << run << ',' << mobility << ','
              << start << ',' << duration << ',' << cleanup << ',' << rateMbps << ',' << packetSize << ','
              << halfSide << ',' << speed << ',' << txPower << ",OfdmRate24Mbps,";
      writeMetrics (summary, total, appTotal);
      summary << ',' << jainsFairness << ',' << zeroTx << ',' << zeroRx << ',' << zeroAppRx << ',' << readyAtStart << ',' << assocMismatch << '\n';
    }

  flows.close (); summary.close (); positionFile.close ();
  NS_ABORT_MSG_IF (!flows || !summary || !positionFile, "Output write failure");
  if (xml) monitor->SerializeToXmlFile (output + "-flowmon.xml", false, false);
  std::cout << "DONE transport=" << transport << " appMode=" << appMode << " tcp=" << tcpVariant
            << " rtsCts=" << (rtsCts ? 1 : 0) << " stations=" << stations << " run=" << run
            << " txIP=" << total.tx << " rxIP=" << total.rx
            << " PDR_IP=" << NumberOrBlank (100.0 * total.rx, total.tx)
            << "% activeGoodput=" << appTotal.rxActive * 8.0 / (duration * 1000) << " Kbps"
            << " JFI=" << jainsFairness << "\n";
#ifdef __linux__
  if (g_liveSock >= 0) close (g_liveSock);
#endif
  Simulator::Destroy ();
  return 0;
}
